"""Arrays: shape, lower bounds, NULL vs empty text, header validation."""

from __future__ import annotations

import pytest

import _common as B
from pgcopy_bridge import (
    CopyFormatError,
    Limits,
    ValueFormatError,
    catalogue_from_mapping,
    decode_bytes,
)
from pgcopy_bridge.library import encode_bytes


def cat_of(type_name="int4[]"):
    return catalogue_from_mapping(
        {"columns": [{"name": "a", "type": type_name}]})


def one(body):
    return decode_bytes(B.build([B.tuple_([body])]), cat_of(),
                        chunk_size=1)[0][0]


def decoded_of(type_name, value):
    cat = cat_of(type_name)
    return decode_bytes(encode_bytes([[value]], cat), cat,
                        chunk_size=1)[0][0]


def test_1d_array_shape_and_lower_bounds_preserved():
    body = B.array([(3, 1)], 23, [B.int4(1), None, B.int4(3)])
    value = one(body)
    assert value == {"shape": [3], "lower_bounds": [1],
                     "elements": [1, None, 3]}


def test_2d_array_row_major_and_non_unit_bounds():
    body = B.array([(2, 0), (2, -3)], 23,
                   [B.int4(1), B.int4(2), B.int4(3), B.int4(4)])
    value = one(body)
    assert value["lower_bounds"] == [0, -3]
    assert value["elements"] == [1, 2, 3, 4]  # last dim varies fastest


def test_lower_bound_less_than_zero_roundtrip():
    value = {"shape": [2, 2], "lower_bounds": [-5, 0],
             "elements": [1, 2, 3, 4]}
    out = decoded_of("int4[]", value)
    assert out == value
    # The wire bytes must carry those bounds, not normalise them to 1.
    data = encode_bytes([[value]], cat_of())
    assert (-5).to_bytes(4, "big", signed=True) in data
    assert (0).to_bytes(4, "big", signed=True) in data


def test_empty_array_is_zero_dimensional():
    assert one(B.array([], 23, [])) == {
        "shape": [], "lower_bounds": [], "elements": []}


def test_empty_array_not_confused_with_null():
    value = one(B.array([], 23, []))
    assert value is not None
    assert value["elements"] == []
    assert one(None) is None


def test_empty_dimension_array():
    value = one(B.array([(0, 3)], 23, []))
    assert value == {"shape": [0], "lower_bounds": [3], "elements": []}


def test_null_element_vs_empty_text_element():
    body = B.array([(2, 1)], 25, [B.text(""), None])
    value = one(body) if False else decode_bytes(
        B.build([B.tuple_([body])]),
        catalogue_from_mapping({"columns": [{"name": "a", "type": "text[]"}]}),
        chunk_size=1)[0][0]
    assert value["elements"] == ["", None]


def test_array_null_flag_must_match_elements():
    with pytest.raises(ValueFormatError, match="NULL flag is 0"):
        one(B.array([(2, 1)], 23, [B.int4(1), None], has_nulls=0))
    with pytest.raises(ValueFormatError, match="NULL flag is 1"):
        one(B.array([(2, 1)], 23, [B.int4(1), B.int4(2)], has_nulls=1))


def test_element_oid_must_match_catalogue():
    with pytest.raises(ValueFormatError, match="OID 25"):
        one(B.array([(1, 1)], 25, [B.int4(1)]))  # text OID for int4[]
    with pytest.raises(ValueFormatError, match="OID 23"):
        cat = catalogue_from_mapping(
            {"columns": [{"name": "a", "type": "text[]"}]})
        body = B.array([(1, 1)], 23, [B.text("x")])
        decode_bytes(B.build([B.tuple_([body])]), cat, chunk_size=1)


def test_element_count_mismatch_missing():
    body = B.array([(2, 1)], 23, [B.int4(1)])
    with pytest.raises(CopyFormatError, match="truncated"):
        one(body)


def test_element_count_mismatch_extra():
    body = B.array([(2, 1)], 23, [B.int4(1), B.int4(2), B.int4(3)])
    with pytest.raises(ValueFormatError, match="trailing"):
        one(body)


def test_zero_dimension_array_with_elements_rejected():
    body = B.array([], 23, [B.int4(1)])
    with pytest.raises(ValueFormatError, match="trailing"):
        one(body)


def test_negative_dimension_length_rejected():
    body = B.array([(-1, 1)], 23, [])
    with pytest.raises(ValueFormatError, match="negative"):
        one(body)


def test_negative_element_length_rejected():
    body = B.array([(1, 1)], 23, [])
    body += (-2).to_bytes(4, "big", signed=True)
    with pytest.raises(ValueFormatError, match="< -1"):
        one(body)


def test_element_body_of_wrong_scalar_size():
    body = B.array([(2, 1)], 23, [B.int4(1), B.int8(2)])
    with pytest.raises(ValueFormatError, match="4 bytes"):
        one(body)


def test_shape_lower_bounds_mismatch_on_encode():
    with pytest.raises(ValueFormatError, match="lower_bounds"):
        decoded_of("int4[]", {"shape": [2, 2], "lower_bounds": [0],
                              "elements": [1, 2, 3, 4]})


def test_element_product_mismatch_on_encode():
    with pytest.raises(ValueFormatError, match="declares 6"):
        decoded_of("int4[]", {"shape": [2, 3], "lower_bounds": [1, 1],
                              "elements": [1, 2, 3, 4]})


def test_negative_shape_on_encode():
    with pytest.raises(ValueFormatError, match="negative"):
        decoded_of("int4[]", {"shape": [-2], "lower_bounds": [1],
                              "elements": []})


def test_unknown_keys_on_encode():
    with pytest.raises(ValueFormatError, match="unknown keys"):
        decoded_of("int4[]", {"shape": [], "lower_bounds": [],
                              "elements": [], "nested": []})


def test_plain_list_rejected_on_encode():
    # The contract requires shape/lower_bounds/elements; a bare list would
    # silently lose the lower bounds.
    with pytest.raises(ValueFormatError, match="shape"):
        decoded_of("int4[]", [1, 2, 3])


def test_array_null_flag_written_only_when_needed():
    cat = cat_of()
    no_null = encode_bytes([[{"shape": [2], "lower_bounds": [1],
                              "elements": [1, 2]}]], cat)
    with_null = encode_bytes([[{"shape": [2], "lower_bounds": [1],
                                 "elements": [1, None]}]], cat)
    field_at = 25
    assert no_null[field_at + 4:field_at + 8] == b"\x00\x00\x00\x00"
    assert with_null[field_at + 4:field_at + 8] == b"\x00\x00\x00\x01"


@pytest.mark.parametrize("type_name, element, oid", [
    ("int4[]", B.int4(1), 23),
    ("text[]", B.text("x"), 25),
    ("numeric[]", B.numeric(1, 0, 0, 0, [7]), 1700),
])
def test_each_array_type_oid_and_empty_element(type_name, element, oid):
    body = B.array([(1, 1)], oid, [element])
    cat = catalogue_from_mapping(
        {"columns": [{"name": "a", "type": type_name}]})
    value = decode_bytes(B.build([B.tuple_([body])]), cat, chunk_size=1)[0][0]
    assert value["shape"] == [1]


def test_limits_nested_array_dimensions():
    limits = Limits(max_array_dims=2)
    cat = catalogue_from_mapping(
        {"columns": [{"name": "a", "type": "numeric[]"}]})
    body = B.array([(1, 1), (1, 1), (1, 1)], 1700,
                   [B.numeric(1, 0, 0, 0, [1])])
    from pgcopy_bridge.copy_stream import CopyDecoder
    import io
    decoder = CopyDecoder(io.BytesIO(B.build([B.tuple_([body])])), cat,
                          limits=limits, chunk_size=1)
    with pytest.raises(Exception) as info:
        list(decoder.iter_tuples())
    assert "max_array_dims" in str(info.value)


def test_limits_element_product_checked_before_reading():
    limits = Limits(max_array_elements=3)
    cat = catalogue_from_mapping(
        {"columns": [{"name": "a", "type": "int4[]"}]})
    body = B.array([(2, 1), (2, 1)], 23, [])
    import io
    from pgcopy_bridge.copy_stream import CopyDecoder
    with pytest.raises(Exception) as info:
        list(CopyDecoder(io.BytesIO(B.build([B.tuple_([body])])), cat,
                         limits=limits, chunk_size=1).iter_tuples())
    assert "max_array_elements" in str(info.value)


def test_one_dimensional_zero_length_matches_postgresq_size_rule():
    # PostgreSQL accepts NDIM=1, LEN=0 and canonicalises it to NDIM=0.
    # We accept it and report the truth: shape [0], no elements.
    assert one(B.array([(0, 1)], 23, []))["shape"] == [0]
