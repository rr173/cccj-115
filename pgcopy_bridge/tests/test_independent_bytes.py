"""Encoder output checked byte-by-byte by the independent inspector.

``independent_checker.py`` parses the wire format with ``struct`` and does
not import :mod:`pgcopy_bridge`.  These tests encode representative values,
then assert on the *bytes*: header, tuple field counts, field lengths,
numeric headers/groups and array headers/dimensions/OIDs.
"""

from __future__ import annotations

import json

import pytest

import _common as B
from independent_checker import BadBytes, inspect
from pgcopy_bridge import catalogue_from_mapping
from pgcopy_bridge.library import encode_bytes

COLUMNS = [
    {"name": "i4", "type": "int4"},
    {"name": "i8", "type": "int8"},
    {"name": "b", "type": "bool"},
    {"name": "tx", "type": "text"},
    {"name": "by", "type": "bytea"},
    {"name": "u", "type": "uuid"},
    {"name": "n", "type": "numeric"},
    {"name": "d", "type": "date"},
    {"name": "ts", "type": "timestamp"},
    {"name": "tz", "type": "timestamptz"},
    {"name": "a4", "type": "int4[]"},
    {"name": "at", "type": "text[]"},
    {"name": "an", "type": "numeric[]"},
]

CATALOGUE = catalogue_from_mapping({"columns": COLUMNS})
TYPES = [c["type"] for c in COLUMNS]


def encode_one(values):
    return encode_bytes([values], CATALOGUE)


def test_header_flags_extension_and_end_marker_are_exact():
    data = encode_one([
        None, None, None, None, None, None, None, None, None, None,
        None, None, None])
    assert data[:11] == B.SIGNATURE
    assert data[11:15] == b"\x00\x00\x00\x00"
    assert data[15:19] == b"\x00\x00\x00\x00"
    assert data[19:21] == (13).to_bytes(2, "big", signed=True)
    # 13 NULL fields -> 13 * 4 bytes, then the end marker
    assert len(data) == 21 + 13 * 4 + 2
    assert data[-2:] == b"\xff\xff"
    walked = inspect(data, TYPES)
    assert walked["tuples"] == [[None] * 13]


def test_numeric_groups_on_the_wire():
    data = encode_one([
        None, None, None, None, None, None,
        "123456789012345678901234567890.1234567890000", None, None, None,
        None, None, None])
    walked = inspect(data, TYPES)
    num = walked["tuples"][0][6]["value"]
    assert num["ndigits"] == 11
    assert num["weight"] == 7
    assert num["sign"] == 0x0000
    assert num["dscale"] == 13
    assert num["digits"] == [12, 3456, 7890, 1234, 5678, 9012, 3456,
                             7890, 1234, 5678, 9000]
    # Independently recompute the integer part from weight and groups.
    # 11 groups with weight 7 means 8 integer groups (32 digits) and 3
    # fractional groups (12 digits); dscale 13 pads one displayed zero.  All
    # arithmetic is on integers: digits concatenated, then split by place.
    fractional_groups = num["ndigits"] - num["weight"] - 1
    numerator = 0
    for index, group in enumerate(num["digits"]):
        numerator = numerator * 10000 + group
    integer = numerator // 10000 ** fractional_groups
    fraction = numerator % 10000 ** fractional_groups
    assert integer == 123456789012345678901234567890
    assert fraction == 123456789000
    assert int(str(integer) + str(fraction).zfill(12)) == \
        123456789012345678901234567890123456789000


def test_numeric_trailing_zero_scale_on_the_wire():
    data = encode_one([None] * 6 + ["0.0000"] + [None] * 6)
    num = inspect(data, TYPES)["tuples"][0][6]["value"]
    assert (num["ndigits"], num["weight"], num["sign"], num["dscale"],
            num["digits"]) == (0, 0, 0x0000, 4, [])


def test_numeric_specials_on_the_wire():
    for text, code in (("NaN", 0xC000), ("Infinity", 0xD000),
                       ("-Infinity", 0xF000)):
        data = encode_one([None] * 6 + [text] + [None] * 6)
        num = inspect(data, TYPES)["tuples"][0][6]["value"]
        assert num["sign"] == code
        assert num["digits"] == []


def test_array_header_dimensions_lower_bounds_and_oids():
    data = encode_one([
        None, None, None, None, None, None, None, None, None, None,
        {"shape": [2, 2], "lower_bounds": [0, -3],
         "elements": [11, None, 13, 14]},
        {"shape": [2], "lower_bounds": [0], "elements": ["", None]},
        {"shape": [], "lower_bounds": [], "elements": []},
    ])
    walked = inspect(data, TYPES)
    a4, at, an = walked["tuples"][0][10:13]
    assert a4["dims"] == [(2, 0), (2, -3)]
    assert a4["elements"] == [11, None, 13, 14]
    assert at["dims"] == [(2, 0)]
    assert at["elements"] == [b"", None]
    assert an["dims"] == []


def test_each_field_length_matches_body():
    values = [
        1, 2, True, "abc", "00ff", "123e4567-e89b-12d3-a456-426614174000",
        "1.25", 3, 4, 5,
        {"shape": [1], "lower_bounds": [1], "elements": [7]},
        {"shape": [1], "lower_bounds": [1], "elements": ["x"]},
        {"shape": [1], "lower_bounds": [1], "elements": ["NaN"]},
    ]
    data = encode_one(values)
    # Walk the length prefixes independently of the checker.
    pos = 19
    count = int.from_bytes(data[pos:pos + 2], "big", signed=True)
    assert count == 13
    pos += 2
    lengths = []
    for _ in range(count):
        length = int.from_bytes(data[pos:pos + 4], "big", signed=True)
        lengths.append(length)
        pos += 4 + length
    assert pos == len(data) - 2
    # int4, int8, bool, "abc", 1-byte hex, uuid, numeric(1.25), date,
    # timestamp, timestamptz, int4[] (12+8+8), text[] (12+8+5),
    # numeric[] (12+8+12)
    assert lengths == [4, 8, 1, 3, 2, 16, 12, 4, 8, 8, 28, 25, 32]
    # And the checker agrees the whole file is legal.
    inspect(data, TYPES)


def test_catalogue_order_is_the_wire_order():
    data = encode_one([
        1, 2, True, "abc", "", "00000000-0000-0000-0000-000000000000",
        "0", 0, 0, 0, None, None, None])
    walked = inspect(data, TYPES)
    row = walked["tuples"][0]
    assert row[0]["value"] == 1
    assert row[3]["value"] == "abc"
    assert row[4]["value"] == b""
    assert row[10] is None


def test_independent_checker_catches_encoder_mistakes():
    good = encode_one([
        1, 2, True, "abc", "", "00000000-0000-0000-0000-000000000000",
        "0", 0, 0, 0, None, None, None])
    mutated = bytearray(good)
    mutated[11:15] = b"\x00\x00\x00\x01"
    with pytest.raises(BadBytes, match="flags"):
        inspect(bytes(mutated), TYPES)
    mutated = bytearray(good)
    mutated[-2:] = b"\x00\x01"
    with pytest.raises(BadBytes):
        inspect(bytes(mutated), TYPES)
    mutated = bytearray(good)
    mutated[19:21] = (12).to_bytes(2, "big", signed=True)
    with pytest.raises(BadBytes, match="field count"):
        inspect(bytes(mutated), TYPES)
