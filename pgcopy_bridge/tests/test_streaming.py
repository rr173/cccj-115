"""Byte-at-a-time, odd block sizes, extension headers, zero rows."""

from __future__ import annotations

import io
import json
import os

import pytest

import _common as B
from pgcopy_bridge import load_catalogue
from pgcopy_bridge.library import decode_file, encode_file


class ByteStream:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, n):
        chunk = self.data[self.pos:self.pos + 1]
        self.pos += 1
        return chunk


class RandomShortStream:
    def __init__(self, data, sizes):
        self.data = data
        self.pos = 0
        self.sizes = list(sizes)
        self.calls = 0

    def read(self, n):
        size = self.sizes[self.calls % len(self.sizes)]
        self.calls += 1
        chunk = self.data[self.pos:self.pos + min(size, n)]
        self.pos += len(chunk)
        return chunk


def sample_records(tmp_path, count=3):
    cat = {"columns": [{"name": "a", "type": "int4"},
                       {"name": "b", "type": "text"},
                       {"name": "c", "type": "numeric"},
                       {"name": "d", "type": "int4[]"}]}
    path = tmp_path / "cat.json"
    path.write_text(json.dumps(cat))
    tuples = []
    for i in range(count):
        tuples.append([i, f"row-{i}", "1.2300",
                       {"shape": [2], "lower_bounds": [0],
                        "elements": [i, None] if i % 2 else [i, i]}])
    return str(path), tuples


def test_one_byte_chunks_for_every_material(materials_dir, tmp_path):
    for name, cat in (("sample", "catalogue.json"),
                      ("arrays", "arrays_catalogue.json")):
        out = tmp_path / f"{name}.jsonl"
        decode_file(os.path.join(materials_dir, f"{name}.copy"),
                    os.path.join(materials_dir, cat), str(out),
                    chunk_size=1)
        expected = open(os.path.join(materials_dir, f"{name}.jsonl"),
                        "rb").read()
        assert out.read_bytes() == expected


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 8, 13, 21, 34, 55, 64, 255,
                                   1024, 65536])
def test_many_chunk_sizes_produce_identical_json(tmp_path, chunk):
    from pgcopy_bridge.library import encode_bytes, decode_bytes
    cat_path, tuples = sample_records(tmp_path)
    catalogue = load_catalogue(cat_path)
    data = encode_bytes(tuples, catalogue)
    decoded = decode_bytes(data, catalogue, chunk_size=chunk)
    assert len(decoded) == len(tuples)
    for row, original in zip(decoded, tuples):
        assert row[0:2] == original[0:2]
        assert row[2]["digits"] == [1, 2300]   # 1.2300 kept exactly
        assert row[3] == original[3]


def test_mixed_short_reads(tmp_path, materials_dir):
    data = open(os.path.join(materials_dir, "sample.copy"), "rb").read()
    catalogue = load_catalogue(os.path.join(materials_dir, "catalogue.json"))
    from pgcopy_bridge.copy_stream import CopyDecoder
    for sizes in ([1], [2, 3], [7, 1, 64], [65536, 1], [5, 4096, 3]):
        decoder = CopyDecoder(RandomShortStream(data, sizes), catalogue,
                              chunk_size=max(sizes))
        rows = list(decoder.iter_tuples())
        assert len(rows) == 2


def test_extension_header_with_one_byte_reads(materials_dir, tmp_path):
    data = B.build(
        [B.tuple_([B.int4(1), B.text("x"), B.numeric(1, 0, 0, 1, [1]),
                   B.array([(1, 0)], 23, [B.int4(9)])])],
        extension=b"PGCOPY-extension-payload" * 3)
    cat_path, _ = sample_records(tmp_path)
    src = tmp_path / "ext.copy"
    src.write_bytes(data)
    out = tmp_path / "out.jsonl"
    decode_file(str(src), cat_path, str(out), chunk_size=1)
    record = json.loads(out.read_text().strip())
    assert record["a"] == 1


def test_empty_file_but_legal_zero_rows_via_cli(tmp_path, materials_dir):
    cat_path = os.path.join(materials_dir, "catalogue.json")
    out = tmp_path / "zero.jsonl"
    decode_file(os.path.join(materials_dir, "empty.copy"), cat_path,
                str(out), chunk_size=1)
    assert out.read_bytes() == b""
    back = tmp_path / "zero.copy"
    encode_file(str(out), cat_path, str(back))
    assert back.read_bytes() == open(
        os.path.join(materials_dir, "empty.copy"), "rb").read()


def test_empty_jsonl_roundtrip(tmp_path, materials_dir):
    cat_path = os.path.join(materials_dir, "catalogue.json")
    empty_jsonl = tmp_path / "empty.jsonl"
    empty_jsonl.write_bytes(b"")
    out = tmp_path / "out.copy"
    encode_file(str(empty_jsonl), cat_path, str(out), chunk_size=1)
    assert out.read_bytes() == open(
        os.path.join(materials_dir, "empty.copy"), "rb").read()


def test_truncation_detected_at_every_cut_point(materials_dir, tmp_path):
    """Cut the good file at every byte and require a located failure."""
    data = open(os.path.join(materials_dir, "sample.copy"), "rb").read()
    from pgcopy_bridge import PgCopyError
    from pgcopy_bridge.copy_stream import CopyDecoder
    catalogue = load_catalogue(os.path.join(materials_dir, "catalogue.json"))
    good_last_two = len(data) - 2
    for cut in range(good_last_two + 1):
        decoder = CopyDecoder(ByteStream(data[:cut]), catalogue, chunk_size=1)
        with pytest.raises(PgCopyError) as info:
            list(decoder.iter_tuples())
        assert info.value.offset is not None
        assert info.value.line is not None


def test_stream_helpers_roundtrip(tmp_path):
    import io
    from pgcopy_bridge import catalogue_from_mapping
    from pgcopy_bridge.copy_stream import decode_stream, encode_stream
    catalogue = catalogue_from_mapping(
        {"columns": [{"name": "a", "type": "int4"}]})
    copy_buf = io.BytesIO()
    assert encode_stream(io.BytesIO(b'{"a":1}\n{"a":2}\n'), catalogue,
                         copy_buf) == 2
    jsonl_buf = io.BytesIO()
    assert decode_stream(io.BytesIO(copy_buf.getvalue()), catalogue,
                         jsonl_buf) == 2
    assert jsonl_buf.getvalue() == b'{"a":1}\n{"a":2}\n'


def test_large_text_field_read_in_one_byte_chunks(tmp_path):
    cat_path, _ = sample_records(tmp_path, count=0)
    catalogue = load_catalogue(cat_path)
    big = "é" * 5000
    data = B.build([B.tuple_([B.int4(7), B.text(big),
                              B.numeric(1, 0, 0, 0, [1]), B.array([], 23, [])])])
    from pgcopy_bridge.copy_stream import CopyDecoder
    rows = list(CopyDecoder(ByteStream(data), catalogue).iter_tuples())
    assert rows[0][1] == big
