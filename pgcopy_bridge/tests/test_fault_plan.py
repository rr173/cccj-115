"""Fault injection and the file-publication contract.

Each test starts from a good file, corrupts exactly one protocol element,
checks the reported byte position and confirms an existing destination is
left byte-for-byte unchanged with no temporary file left behind.
"""

from __future__ import annotations

import json
import os

import pytest

import _common as B
from pgcopy_bridge import PgCopyError, load_catalogue
from pgcopy_bridge.library import decode_file, encode_file, inspect_file


def _write(path, data: bytes) -> str:
    with open(path, "wb") as fh:
        fh.write(data)
    return str(path)


def _temp_files(directory):
    return sorted(name for name in os.listdir(directory)
                  if name.endswith(".tmp"))


@pytest.fixture()
def good_bytes(materials_dir):
    return open(os.path.join(materials_dir, "sample.copy"), "rb").read()


def _decode_fails(tmp_path, catalogue, data, chunk_size=1):
    src = _write(tmp_path / "corrupt.copy", data)
    dest = tmp_path / "dest.jsonl"
    dest.write_bytes(b"KEEP-ME\n")
    before = dest.read_bytes()
    with pytest.raises(PgCopyError) as info:
        decode_file(src, catalogue, str(dest), chunk_size=chunk_size)
    assert dest.read_bytes() == before
    assert _temp_files(str(tmp_path)) == []
    return info.value


def _encode_fails(tmp_path, catalogue, jsonl_bytes):
    src = _write(tmp_path / "bad.jsonl", jsonl_bytes)
    dest = tmp_path / "dest.copy"
    dest.write_bytes(b"KEEP-COPY")
    before = dest.read_bytes()
    with pytest.raises(PgCopyError) as info:
        encode_file(src, catalogue, str(dest))
    assert dest.read_bytes() == before
    assert _temp_files(str(tmp_path)) == []
    return info.value


@pytest.mark.parametrize("chunk", [1, 2, 7, 65536])
def test_bad_signature_is_located(good_bytes, catalogue_path, tmp_path,
                                  chunk):
    data = bytearray(good_bytes)
    data[3] ^= 0xFF
    error = _decode_fails(tmp_path, catalogue_path, bytes(data), chunk)
    assert error.offset == 0
    assert "signature" in str(error)


def test_bad_flags_is_located(good_bytes, catalogue_path, tmp_path):
    data = bytearray(good_bytes)
    data[11:15] = (1).to_bytes(4, "big", signed=True)
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    assert error.offset == 11
    assert "flags" in str(error)


def test_bad_field_length_negative(good_bytes, catalogue_path, tmp_path):
    data = bytearray(good_bytes)
    data[25:29] = (-5).to_bytes(4, "big", signed=True)  # neg field length
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    assert error.offset == 25
    assert error.line == 1
    assert error.field == 2
    assert "less than -1" in str(error)


def test_bad_field_length_too_large_for_body(good_bytes, catalogue_path,
                                             tmp_path):
    data = bytearray(good_bytes)
    data[25:29] = (10_000).to_bytes(4, "big", signed=True)
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    # the length is plausible for the limit, so truncation is reported at
    # the start of the body it could not fill
    assert error.offset == 29


def test_numeric_group_corruption(good_bytes, catalogue_path, tmp_path):
    data = bytearray(good_bytes)
    # amount body starts at 73, groups at 81; group 0 = 12
    data[81:83] = (12345).to_bytes(2, "big")
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    assert error.offset == 73 + 8
    assert error.line == 1
    assert error.field == 6
    assert "9999" in str(error)


def test_numeric_sign_corruption(good_bytes, catalogue_path, tmp_path):
    data = bytearray(good_bytes)
    data[77:79] = b"\x12\x34"
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    assert error.offset == 73 + 4
    assert "sign" in str(error)


def test_numeric_length_group_count_mismatch(good_bytes, catalogue_path,
                                             tmp_path):
    data = bytearray(good_bytes)
    data[73:75] = (12).to_bytes(2, "big", signed=True)  # was 11
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    assert "requires" in str(error)
    assert error.offset == 73


def test_array_oid_corruption(good_bytes, catalogue_path, tmp_path):
    data = bytearray(good_bytes)
    # grid body begins at 278: ndim, hasnull, oid
    data[286:290] = (25).to_bytes(4, "big", signed=True)
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    assert error.offset == 278 + 8
    assert error.line == 2
    assert error.field == 11
    assert "OID" in str(error)


def test_array_shape_corruption(good_bytes, catalogue_path, tmp_path):
    data = bytearray(good_bytes)
    # first dimension length at 290; claim 3x2 = 6 elements instead of 4
    data[290:294] = (3).to_bytes(4, "big", signed=True)
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    assert "truncated" in str(error) or "trailing" in str(error)


def test_array_negative_dimension(good_bytes, catalogue_path, tmp_path):
    data = bytearray(good_bytes)
    data[290:294] = (-1).to_bytes(4, "big", signed=True)
    error = _decode_fails(tmp_path, catalogue_path, bytes(data))
    assert "negative" in str(error)


def test_truncated_tail(good_bytes, catalogue_path, tmp_path):
    data = good_bytes[:-1]
    error = _decode_fails(tmp_path, catalogue_path, data)
    assert "truncated" in str(error)
    assert error.offset == len(data) - 1  # one byte of the end marker
    assert "end" not in str(error) or "field count" in str(error)


def test_extra_bytes_after_end_marker(good_bytes, catalogue_path, tmp_path):
    error = _decode_fails(tmp_path, catalogue_path, good_bytes + b"\x00\x00")
    assert "extra" in str(error)
    assert error.offset == len(good_bytes)


def test_missing_end_marker(good_bytes, catalogue_path, tmp_path):
    error = _decode_fails(tmp_path, catalogue_path, good_bytes[:-2])
    assert error.line == 3  # the missing next tuple count


def test_inspect_fails_on_bad_file_preserving_outputs(good_bytes,
                                                      catalogue_path,
                                                      tmp_path):
    data = bytearray(good_bytes)
    data[286:290] = (1700).to_bytes(4, "big", signed=True)
    src = _write(tmp_path / "bad.copy", bytes(data))
    dest = tmp_path / "unused.txt"
    dest.write_bytes(b"existing")
    with pytest.raises(PgCopyError):
        inspect_file(src, catalogue_path, chunk_size=1)
    assert dest.read_bytes() == b"existing"


# ----------------------------------------------------------- encode side

GOOD_RECORDS = [
    {"id": 1, "neg": 2, "label": "a", "raw": "", "uid": "00000000-0000-0000-0000-000000000000",
     "amount": "1.0", "zero4": "0.0000", "d": 0, "ts": 0, "tz": 0,
     "grid": None, "notes": None, "ranks": None},
    {"id": 2, "neg": 3, "label": "b", "raw": "ff", "uid": "00000000-0000-0000-0000-000000000001",
     "amount": "2.5", "zero4": "1.25", "d": 1, "ts": 1, "tz": 1,
     "grid": {"shape": [1], "lower_bounds": [1], "elements": [7]},
     "notes": None, "ranks": None},
]


def _jsonl(records):
    return "".join(json.dumps(r) + "\n" for r in records).encode()


def test_encode_failure_after_some_records_keeps_destination(catalogue_path,
                                                             tmp_path):
    bad = GOOD_RECORDS + [dict(GOOD_RECORDS[0], amount=1.5)]
    error = _encode_fails(tmp_path, catalogue_path, _jsonl(bad))
    assert error.line == 3
    assert "float" in str(error).lower()


def test_encode_numeric_group_failure_keeps_destination(catalogue_path,
                                                        tmp_path):
    record = dict(GOOD_RECORDS[0])
    record["amount"] = {"ndigits": 1, "weight": 0, "sign": 0, "dscale": 0,
                        "digits": [10000]}
    error = _encode_fails(tmp_path, catalogue_path, _jsonl([record]))
    assert "9999" in str(error)


def test_encode_array_shape_failure_keeps_destination(catalogue_path,
                                                      tmp_path):
    record = dict(GOOD_RECORDS[0])
    record["grid"] = {"shape": [2, 2], "lower_bounds": [1, 1],
                      "elements": [1, 2, 3]}
    error = _encode_fails(tmp_path, catalogue_path, _jsonl([record]))
    assert "declares" in str(error)


def test_encode_missing_field_keeps_destination(catalogue_path, tmp_path):
    record = dict(GOOD_RECORDS[0])
    del record["tz"]
    error = _encode_fails(tmp_path, catalogue_path, _jsonl([record]))
    assert "missing" in str(error)


def test_encode_unknown_field_keeps_destination(catalogue_path, tmp_path):
    record = dict(GOOD_RECORDS[0], surprise=1)
    error = _encode_fails(tmp_path, catalogue_path, _jsonl([record]))
    assert "unknown" in str(error)


def test_encode_duplicate_json_key_keeps_destination(catalogue_path,
                                                     tmp_path):
    payload = b'{"id":1,"id":2}\n'
    error = _encode_fails(tmp_path, catalogue_path, payload)
    assert "duplicate key" in str(error)


def test_encode_invalid_json_keeps_destination(catalogue_path, tmp_path):
    error = _encode_fails(tmp_path, catalogue_path, b"{not json}\n")
    assert "invalid JSON" in str(error)


def test_encode_truncated_last_line_without_newline_is_still_checked(
        catalogue_path, tmp_path):
    # A final line without \n is a record; here it is truncated JSON.
    error = _encode_fails(tmp_path, catalogue_path,
                          b'{"id": 1, "neg": 2')
    assert error.line == 1


def test_encode_empty_input_produces_legal_zero_row_file(
        catalogue_path, tmp_path):
    src = tmp_path / "empty.jsonl"
    src.write_bytes(b"")
    out = tmp_path / "out.copy"
    encode_file(str(src), catalogue_path, str(out))
    assert out.read_bytes() == B.build([])


def test_decode_success_replaces_destination(catalogue_path, materials_dir,
                                             tmp_path):
    dest = tmp_path / "out.jsonl"
    dest.write_bytes(b"OLD CONTENT THAT MUST GO")
    src = os.path.join(materials_dir, "sample.copy")
    decode_file(src, catalogue_path, str(dest), chunk_size=1)
    assert dest.read_bytes() != b"OLD CONTENT THAT MUST GO"
    assert dest.read_bytes().count(b"\n") == 2
    assert _temp_files(str(tmp_path)) == []


def test_stdin_decode_publishes_only_after_full_validation(catalogue_path,
                                                           tmp_path,
                                                           monkeypatch):
    # Feed a corrupt COPY through stdio: the destination must not change,
    # which is the standard-input form of the same contract.
    import io
    import sys
    from pgcopy_bridge import library

    good = open(os.path.join(os.path.dirname(catalogue_path),
                             "sample.copy"), "rb").read()
    monkeypatch.setattr(sys, "stdin",
                        type("S", (), {"buffer": io.BytesIO(good[:-1])})())
    dest = tmp_path / "stdin.jsonl"
    dest.write_bytes(b"PRIOR")
    with pytest.raises(PgCopyError):
        library.decode_file("-", catalogue_path, str(dest), chunk_size=3)
    assert dest.read_bytes() == b"PRIOR"
    assert _temp_files(str(tmp_path)) == []


def test_stdin_encode_publishes_after_last_record(catalogue_path, tmp_path,
                                                  monkeypatch):
    import io
    import sys
    from pgcopy_bridge import library

    payload = _jsonl(GOOD_RECORDS)
    monkeypatch.setattr(sys, "stdin",
                        type("S", (), {"buffer": io.BytesIO(payload)})())
    dest = tmp_path / "stdin.copy"
    library.encode_file("-", catalogue_path, str(dest))
    assert dest.read_bytes() == B.build([
        B.tuple_([
            B.int4(1), B.int8(2), B.text("a"), B.bytea(b""),
            B.uuid_hex("00000000-0000-0000-0000-000000000000"),
            B.numeric(1, 0, 0, 1, [1]), B.numeric(0, 0, 0, 4, []),
            B.date(0), B.timestamp(0), B.timestamp(0),
            None, None, None]),
        B.tuple_([
            B.int4(2), B.int8(3), B.text("b"), B.bytea(b"\xff"),
            B.uuid_hex("00000000-0000-0000-0000-000000000001"),
            B.numeric(2, 0, 0, 1, [2, 5000]), B.numeric(2, 0, 0, 2, [1, 2500]),
            B.date(1), B.timestamp(1), B.timestamp(1),
            B.array([(1, 1)], 23, [B.int4(7)]), None, None]),
    ])


def test_multibyte_line_split_across_reads(catalogue_path, tmp_path):
    # A UTF-8 character split between two input chunks must still parse.
    record = dict(GOOD_RECORDS[0], label="é中")
    payload = _jsonl([record])
    src = tmp_path / "in.jsonl"
    src.write_bytes(payload)
    out = tmp_path / "out.copy"
    for chunk in (1, 2, 3, 65536):
        encode_file(str(src), catalogue_path, str(out), chunk_size=chunk)
        assert out.read_bytes()[:11] == B.SIGNATURE
