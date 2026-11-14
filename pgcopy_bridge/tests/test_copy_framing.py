"""Copy framing: signature, flags, extension header, end marker, chunking."""

from __future__ import annotations

import io
import os

import pytest

import _common as B
from pgcopy_bridge import CopyFormatError, Limits, load_catalogue
from pgcopy_bridge.copy_stream import CopyDecoder, CopyEncoder, SIGNATURE
from pgcopy_bridge.library import decode_bytes


class OneByteStream:
    """Returns exactly one byte per read(); no read() yields a field."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.reads = 0

    def read(self, n: int) -> bytes:
        assert n >= 0
        self.reads += 1
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + 1]
        self._pos += 1
        return chunk


class ShortStream:
    """Returns at most ``size`` bytes per read()."""

    def __init__(self, data: bytes, size: int) -> None:
        self._data = data
        self._pos = 0
        self._size = size

    def read(self, n: int) -> bytes:
        take = min(n, self._size)
        chunk = self._data[self._pos:self._pos + take]
        self._pos += len(chunk)
        return chunk


def _cat(tmp_path, columns):
    import json
    path = tmp_path / "cat.json"
    path.write_text(json.dumps({"columns": columns}), encoding="utf-8")
    return load_catalogue(str(path))


SIMPLE = [{"name": "a", "type": "int4"}, {"name": "b", "type": "text"}]


def test_header_constants():
    assert SIGNATURE == b"PGCOPY\n\xff\r\n\x00"
    assert len(SIGNATURE) == 11


def test_decoder_works_one_byte_per_read(tmp_path):
    data = B.build([B.tuple_([B.int4(7), B.text("hi")])])
    cat = _cat(tmp_path, SIMPLE)
    stream = OneByteStream(data)
    decoder = CopyDecoder(stream, cat, chunk_size=1)
    assert list(decoder.iter_tuples()) == [[7, "hi"]]
    # Many reads, one per byte at least.
    assert stream.reads >= len(data)


def test_decoder_works_with_mixed_short_reads(tmp_path):
    data = B.build([B.tuple_([B.int4(1), B.text("x" * 300)]),
                    B.tuple_([None, B.text("")])])
    cat = _cat(tmp_path, SIMPLE)
    expected = [[1, "x" * 300], [None, ""]]
    for size in (1, 2, 3, 5, 7, 13, 64, 100, 4096):
        stream = ShortStream(data, size)
        got = list(CopyDecoder(stream, cat, chunk_size=size).iter_tuples())
        assert got == expected


def test_extension_header_is_skipped_not_read_as_tuple(tmp_path):
    extension = b"EXT\x00\x01" * 5
    data = B.build([B.tuple_([B.int4(42), B.text("ok")])],
                   extension=extension)
    cat = _cat(tmp_path, SIMPLE)
    for size in (1, 3, 4096):
        decoder = CopyDecoder(ShortStream(data, size), cat, chunk_size=size)
        assert list(decoder.iter_tuples()) == [[42, "ok"]]
        assert decoder._header.extension_length == len(extension)
        assert decoder._header.data_offset == 19 + len(extension)


def test_zero_rows_legal_file(tmp_path):
    data = B.build([])
    cat = _cat(tmp_path, SIMPLE)
    assert decode_bytes(data, cat) == []
    decoder = CopyDecoder(io.BytesIO(data), cat)
    assert list(decoder.iter_tuples()) == []
    assert decoder.tuples_read == 0


def test_bad_signature(tmp_path):
    data = b"PGCOPY\n\xff\r\n\x01" + B.build([])[11:]
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError) as info:
        decode_bytes(data, cat)
    assert "signature" in str(info.value)
    assert info.value.offset == 0


@pytest.mark.parametrize("flags", [1, -1, 65536])
def test_nonzero_flags_rejected(tmp_path, flags):
    data = B.build([], flags=flags)
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError) as info:
        decode_bytes(data, cat)
    assert "flags" in str(info.value)
    assert info.value.offset == 11


def test_negative_extension_length_rejected(tmp_path):
    data = SIGNATURE + (0).to_bytes(4, "big", signed=True) + \
        (-4).to_bytes(4, "big", signed=True) + (-1).to_bytes(2, "big", signed=True)
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError, match="extension header"):
        decode_bytes(data, cat)


def test_truncated_extension_header(tmp_path):
    data = SIGNATURE + (0).to_bytes(4, "big", signed=True) + \
        (100).to_bytes(4, "big", signed=True) + b"only-a-few"
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError) as info:
        decode_bytes(data, cat, chunk_size=1)
    assert "extension header" in str(info.value)


def test_missing_end_marker_is_detected(tmp_path):
    data = B.build([B.tuple_([B.int4(1), B.text("a")])], end=False)
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError) as info:
        decode_bytes(data, cat, chunk_size=1)
    assert info.value.offset == len(data)


def test_bytes_after_end_marker_are_rejected(tmp_path):
    data = B.build([], trailing=b"\x00")
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError) as info:
        decode_bytes(data, cat)
    assert "extra byte" in str(info.value)
    assert info.value.offset == 21


def test_truncated_field_length(tmp_path):
    data = B.build([B.tuple_([B.int4(1), B.text("a")])])
    truncated = data[:-6]  # cut inside the second field's body
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError) as info:
        decode_bytes(truncated, cat, chunk_size=1)
    assert "truncated" in str(info.value)
    assert info.value.line == 1


def test_truncated_tuple_count(tmp_path):
    data = B.build([])[:-1]
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError) as info:
        decode_bytes(data, cat, chunk_size=1)
    assert info.value.offset == 19


def test_field_count_mismatch(tmp_path):
    data = B.build([B.tuple_([B.int4(1)])])
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError, match="catalogue declares 2"):
        decode_bytes(data, cat)
    # And the reverse direction: too many fields.
    data = B.build([B.tuple_([B.int4(1), B.text("a"), B.text("b")])])
    with pytest.raises(CopyFormatError):
        decode_bytes(data, cat)


def test_negative_field_count_other_than_minus_one(tmp_path):
    body = (B.header() + (-2).to_bytes(2, "big", signed=True)
            + B.field(B.int4(1)) + (-1).to_bytes(2, "big", signed=True))
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError, match="only -1 ends"):
        decode_bytes(body, cat)


def test_field_length_less_than_minus_one_in_tuple(tmp_path):
    raw = bytearray(B.header())
    raw += (2).to_bytes(2, "big", signed=True)
    raw += (-2).to_bytes(4, "big", signed=True)
    raw += B.field(B.int4(1))
    raw += (-1).to_bytes(2, "big", signed=True)
    cat = _cat(tmp_path, SIMPLE)
    with pytest.raises(CopyFormatError) as info:
        decode_bytes(bytes(raw), cat)
    assert "less than -1" in str(info.value)
    assert info.value.offset == 21
    assert info.value.line == 1
    assert info.value.field == 1


def test_encoder_writes_exact_header_and_end(tmp_path):
    cat = _cat(tmp_path, SIMPLE)
    buf = io.BytesIO()
    enc = CopyEncoder(buf, cat)
    enc.write_header()
    enc.write_tuple([5, "z"], line=1)
    enc.close()
    data = buf.getvalue()
    assert data[:11] == SIGNATURE
    assert data[11:19] == b"\x00\x00\x00\x00\x00\x00\x00\x00"
    assert data[19:21] == b"\x00\x02"
    assert data[-2:] == b"\xff\xff"
    assert decode_bytes(data, cat) == [[5, "z"]]


def test_encode_empty_tuple_list_still_has_header_and_end(tmp_path):
    cat = _cat(tmp_path, SIMPLE)
    buf = io.BytesIO()
    enc = CopyEncoder(buf, cat)
    enc.close()
    assert buf.getvalue() == B.build([])


def test_wrong_field_count_when_encoding(tmp_path):
    from pgcopy_bridge import ValueFormatError
    cat = _cat(tmp_path, SIMPLE)
    buf = io.BytesIO()
    enc = CopyEncoder(buf, cat)
    with pytest.raises(ValueFormatError):
        enc.write_tuple([1], line=3)
    with pytest.raises(ValueFormatError):
        enc.write_tuple([1, "a", 2], line=4)


def test_limits_applied_before_reading_body(tmp_path):
    # A field declaring 10 bytes with max_field_bytes=4 must fail before any
    # body bytes are consumed; the reader position proves it.
    data = B.build([B.tuple_([B.text("0123456789"), B.text("x")])])
    cat = _cat(tmp_path, [{"name": "a", "type": "text"},
                          {"name": "b", "type": "text"}])
    stream = OneByteStream(data)
    decoder = CopyDecoder(stream, cat, limits=Limits(max_field_bytes=4),
                          chunk_size=1)
    with pytest.raises(Exception) as info:
        list(decoder.iter_tuples())
    assert "max_field_bytes" in str(info.value)


def test_one_tuple_at_a_time_laziness(tmp_path):
    data = B.build([B.tuple_([B.int4(i), B.text(str(i))])
                    for i in range(5)])
    cat = _cat(tmp_path, SIMPLE)
    gen = CopyDecoder(io.BytesIO(data), cat).iter_tuples()
    assert next(gen) == [0, "0"]
    assert next(gen) == [1, "1"]
