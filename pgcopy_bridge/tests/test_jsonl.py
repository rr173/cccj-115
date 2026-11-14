"""JSONL carrier: exact integers, no floats, catalogue order, arrow of NULL."""

from __future__ import annotations

import io
import json

import pytest

from pgcopy_bridge import RecordError, catalogue_from_mapping
from pgcopy_bridge.jsonl import LineReader, iter_records, parse_record


def cat(*types):
    return catalogue_from_mapping(
        {"columns": [{"name": f"c{i}", "type": t}
                     for i, t in enumerate(types)]})


def records(payload: bytes, catalogue, **kwargs):
    return list(iter_records(io.BytesIO(payload), catalogue, **kwargs))


def test_output_field_order_follows_catalogue():
    catalogue = cat("int4", "text", "bool")
    values = parse_record(b'{"c2": true, "c0": 1, "c1": "x"}', catalogue, 1)
    assert values == [1, "x", True]


def test_missing_and_unknown_keys_rejected():
    catalogue = cat("int4", "text")
    with pytest.raises(RecordError, match="missing"):
        parse_record(b'{"c0": 1}', catalogue, 1)
    with pytest.raises(RecordError, match="unknown"):
        parse_record(b'{"c0": 1, "c1": "a", "c2": 2}', catalogue, 1)


def test_duplicate_key_rejected():
    catalogue = cat("int4")
    with pytest.raises(RecordError, match="duplicate key"):
        parse_record(b'{"c0": 1, "c0": 2}', catalogue, 1)


def test_json_float_literals_rejected_anywhere():
    catalogue = cat("numeric", "text[]")
    with pytest.raises(RecordError, match="floating-point"):
        parse_record(b'{"c0": 1.5, "c1": null}', catalogue, 1)
    with pytest.raises(RecordError, match="floating-point"):
        parse_record(
            b'{"c0": null, "c1": {"shape":[1],"lower_bounds":[1],'
            b'"elements":[1.0]}}', catalogue, 1)


def test_bare_nan_and_infinity_rejected():
    catalogue = cat("numeric")
    for literal in (b"NaN", b"Infinity", b"-Infinity"):
        with pytest.raises(RecordError, match="floating-point"):
            parse_record(b'{"c0": ' + literal + b"}", catalogue, 1)


def test_quoted_numeric_special_is_kept():
    values = parse_record(b'{"c0": "NaN"}', cat("numeric"), 1)
    assert values == ["NaN"]


def test_integer_beyond_float_precision_kept_exactly():
    catalogue = cat("int8")
    values = parse_record(b'{"c0": 9007199254740993}', catalogue, 1)
    assert values[0] == 9007199254740993
    assert isinstance(values[0], int)


def test_integer_too_large_is_not_silently_floated():
    # Python json parses huge integer literals as int; our encoder then
    # range-checks them.
    catalogue = cat("int8")
    values = parse_record(b'{"c0": 123456789012345678901234567890}',
                          catalogue, 1)
    assert isinstance(values[0], int)


def test_null_vs_empty_string_are_distinct_carriers():
    catalogue = cat("text", "bytea")
    values = parse_record(b'{"c0": "", "c1": ""}', catalogue, 1)
    assert values == ["", ""]
    values = parse_record(b'{"c0": null, "c1": null}', catalogue, 1)
    assert values == [None, None]


def test_blank_lines_rejected_with_line_number():
    with pytest.raises(RecordError, match="blank") as info:
        records(b'{"c0":1}\n\n', cat("int4"))
    assert info.value.line == 2


def test_invalid_utf8_rejected():
    with pytest.raises(RecordError, match="UTF-8"):
        records(b'{"c0": "\xff"}\n', cat("text"))


def test_trailing_crlf_accepted():
    assert records(b'{"c0": 1}\r\n{"c0": 2}\r\n', cat("int4")) == [
        (1, [1]), (2, [2])]


def test_last_line_without_newline_is_parsed():
    assert records(b'{"c0": 1}\n{"c0": 2}', cat("int4")) == [
        (1, [1]), (2, [2])]


def test_line_reader_works_one_byte_at_a_time():
    payload = b'{"a": "\xc3\xa9"}\n{"b": 2}\n'
    reader = LineReader(io.BytesIO(payload), chunk_size=1)
    lines = list(reader)
    assert lines[0][1] == b'{"a": "\xc3\xa9"}'
    assert lines[1][1] == b'{"b": 2}'


def test_line_reader_tracks_byte_offsets():
    payload = b'{"c0": 1}\n{"c0": 2}\n'
    lines = list(LineReader(io.BytesIO(payload), chunk_size=1))
    assert lines[0][2] == 0
    assert lines[1][2] == 10


def test_max_line_bytes_enforced_incrementally():
    catalogue = cat("text")
    payload = b'{"c0": "' + b"x" * 1000 + b'"}\n'
    with pytest.raises(RecordError, match="max_record_bytes"):
        list(iter_records(io.BytesIO(payload), catalogue,
                          max_record_bytes=100, chunk_size=1))


def test_record_must_be_object():
    with pytest.raises(RecordError, match="JSON object"):
        parse_record(b"[1, 2]", cat("int4"), 1)


def test_nested_numeric_object_is_checked_by_encoder_not_jsonl():
    catalogue = cat("numeric")
    value = parse_record(
        b'{"c0": {"ndigits": 1, "weight": 0, "sign": 0, "dscale": 1, '
        b'"digits": [15]}}', catalogue, 1)
    assert value[0]["ndigits"] == 1


def test_json_fields_are_utf8_and_not_escaped_on_output(tmp_path):
    from pgcopy_bridge.catalogue import load_catalogue
    from pgcopy_bridge.library import decode_file
    from tests.conftest import MATERIALS
    import os
    out = tmp_path / "out.jsonl"
    # Build a one-tuple COPY with non-ASCII text using hand bytes.
    import _common as B
    data = B.build([B.tuple_([B.text("é中\U0001f600")])])
    src = tmp_path / "in.copy"
    src.write_bytes(data)
    cat_path = tmp_path / "cat.json"
    cat_path.write_text(json.dumps({"columns": [{"name": "t",
                                                 "type": "text"}]}),
                        encoding="utf-8")
    decode_file(str(src), str(cat_path), str(out))
    raw = out.read_bytes()
    assert "é中\U0001f600".encode("utf-8") in raw
    assert b"\\u" not in raw
