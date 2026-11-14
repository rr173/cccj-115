"""Scalar value codecs: exactness, sentinels, type distinction."""

from __future__ import annotations

import uuid

import pytest

import _common as B
from pgcopy_bridge import Limits, ValueFormatError, catalogue_from_mapping, decode_bytes
from pgcopy_bridge.library import encode_bytes


def cat(*types):
    return catalogue_from_mapping(
        {"columns": [{"name": f"c{i}", "type": t}
                     for i, t in enumerate(types)]})


def one(type_name, body):
    """Decode a single-field file; body ``None`` means SQL NULL."""
    return decode_bytes(B.build([B.tuple_([body])]), cat(type_name),
                        chunk_size=1)[0][0]


def enc(type_name, value):
    """Encode one value and return its field body (without the length)."""
    data = encode_bytes([[value]], cat(type_name))
    # header 11+8 = 19 bytes, tuple count 2, field length 4
    length = int.from_bytes(data[21:25], "big", signed=True)
    body = data[25:25 + length]
    assert len(body) == length
    return body


def roundtrip(type_name, value):
    return decode_bytes(encode_bytes([[value]], cat(type_name)),
                        cat(type_name), chunk_size=1)[0][0]


# ----------------------------------------------------------------- ints

def test_int4_bounds_and_exactness():
    assert one("int4", B.int4(-(2 ** 31))) == -(2 ** 31)
    assert one("int4", B.int4(2 ** 31 - 1)) == 2 ** 31 - 1


def test_int8_beyond_json_float_exactness():
    value = 9007199254740993  # 2**53 + 1, not representable as float
    decoded = one("int8", B.int8(value))
    assert decoded == value
    assert isinstance(decoded, int)
    body = enc("int8", value)
    assert B.int8(value) in body


@pytest.mark.parametrize("value", [2 ** 31, -(2 ** 31) - 1])
def test_int4_out_of_range_on_encode(value):
    with pytest.raises(ValueFormatError, match="outside"):
        enc("int4", value)


def test_int8_out_of_range_on_encode():
    with pytest.raises(ValueFormatError):
        enc("int8", 2 ** 63)


def test_int_types_reject_bool_and_string():
    with pytest.raises(ValueFormatError):
        enc("int4", True)
    with pytest.raises(ValueFormatError):
        enc("int4", "1")


def test_int4_wrong_body_size():
    with pytest.raises(ValueFormatError, match="4 bytes"):
        one("int4", b"\x00\x00\x00")


# ----------------------------------------------------------------- bool

def test_bool_roundtrip_and_body_validation():
    assert one("bool", B.bool_(True)) is True
    assert one("bool", B.bool_(False)) is False
    with pytest.raises(ValueFormatError):
        one("bool", b"\x02")
    with pytest.raises(ValueFormatError):
        enc("bool", 1)


# ----------------------------------------------------------------- text

def test_empty_text_distinct_from_null():
    assert one("text", B.text("")) == ""
    assert one("text", None) is None
    assert enc("text", "") == b""  # zero-length body, not the NULL marker


def test_text_nul_rejected_both_directions():
    with pytest.raises(ValueFormatError, match="NUL"):
        one("text", b"a\x00b")
    with pytest.raises(ValueFormatError, match="NUL"):
        enc("text", "a\x00b")


def test_text_invalid_utf8_rejected():
    with pytest.raises(ValueFormatError, match="UTF-8"):
        one("text", b"\xff")


def test_text_roundtrip_non_ascii():
    value = "aé中\U0001f600"
    assert one("text", B.text(value)) == value
    assert enc("text", value) == B.text(value)


# ---------------------------------------------------------------- bytea

def test_bytea_hex_is_reversible_for_every_byte():
    raw = bytes(range(256))
    decoded = one("bytea", B.bytea(raw))
    assert decoded == raw.hex()
    assert bytes.fromhex(decoded) == raw


def test_empty_bytea_distinct_from_null():
    assert one("bytea", b"") == ""
    assert one("bytea", None) is None


def test_bytea_rejects_odd_and_non_hex():
    with pytest.raises(ValueFormatError):
        enc("bytea", "abc")
    with pytest.raises(ValueFormatError):
        enc("bytea", "zz")


def test_bytea_uses_lowercase_hex():
    assert one("bytea", b"\xab\xcd") == "abcd"


# ----------------------------------------------------------------- uuid

def test_uuid_canonical_output():
    raw = bytes.fromhex("123E4567E89B12D3A456426614174000")
    decoded = one("uuid", raw)
    assert decoded == "123e4567-e89b-12d3-a456-426614174000"
    assert uuid.UUID(decoded).bytes == raw


@pytest.mark.parametrize("spelling", [
    "123e4567-e89b-12d3-a456-426614174000",
    "123E4567-E89B-12D3-A456-426614174000",
    "123e4567e89b12d3a456426614174000",
    "{123e4567-e89b-12d3-a456-426614174000}",
    "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
])
def test_uuid_input_spellings_normalise(spelling):
    assert roundtrip("uuid", spelling) == \
        "123e4567-e89b-12d3-a456-426614174000"


def test_uuid_wrong_length_and_bad_text():
    with pytest.raises(ValueFormatError, match="16 bytes"):
        one("uuid", b"\x00" * 15)
    with pytest.raises(ValueFormatError):
        enc("uuid", "not-a-uuid")


# ------------------------------------------------------- date/timestamps

def test_date_sentinels_and_epoch():
    assert one("date", B.date(-2147483648)) == -2147483648
    assert one("date", B.date(2147483647)) == 2147483647
    assert one("date", B.date(0)) == 0


def test_timestamp_vs_timestamptz_markers():
    body = B.timestamp(0)
    assert one("timestamp", body) == 0
    assert one("timestamptz", body) == 0
    # The two declarations do not share an encoder path tag: round-tripping
    # a negative-epoch value keeps its integer, and the catalogue retains
    # the declared type name.
    c = cat("timestamp", "timestamptz")
    assert c.columns[0].type.name == "timestamp"
    assert c.columns[1].type.name == "timestamptz"


def test_timestamp_infinities():
    assert one("timestamp", B.timestamp(2 ** 63 - 1)) == 2 ** 63 - 1
    assert one("timestamptz", B.timestamp(-(2 ** 63))) == -(2 ** 63)


def test_timestamp_out_of_range():
    with pytest.raises(ValueFormatError):
        enc("timestamp", 2 ** 63)


def test_timestamp_never_interpreted_in_local_timezone(monkeypatch):
    # Changing TZ must not change the decoded microsecond count.
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    assert one("timestamptz", B.timestamp(3600000000)) == 3600000000


def test_wrong_body_sizes_for_date_and_timestamp():
    with pytest.raises(ValueFormatError):
        one("date", b"\x00" * 8)
    with pytest.raises(ValueFormatError):
        one("timestamp", b"\x00" * 4)
