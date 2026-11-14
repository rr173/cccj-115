"""numeric: exact base-10000 semantics, no floats anywhere."""

from __future__ import annotations

import io
import math

import pytest

import _common as B
from pgcopy_bridge import ValueFormatError, catalogue_from_mapping, decode_bytes
from pgcopy_bridge.library import encode_bytes
from pgcopy_bridge.numeric import (
    SIGN_NAN, SIGN_NEG, SIGN_NINF, SIGN_PINF, SIGN_POS, decode, encode,
    to_text,
)


def cat(*types):
    return catalogue_from_mapping(
        {"columns": [{"name": f"c{i}", "type": t}
                     for i, t in enumerate(types)]})


def num_cat():
    return cat("numeric")


def one(body):
    return decode_bytes(B.build([B.tuple_([body])]), num_cat(),
                        chunk_size=1)[0][0]


def body_of(value):
    data = encode_bytes([[value]], num_cat())
    length = int.from_bytes(data[21:25], "big", signed=True)
    return data[25:25 + length]


def decoded_of(value):
    return decode_bytes(encode_bytes([[value]], num_cat()), num_cat(),
                        chunk_size=1)[0][0]


# ------------------------------------------------------------ wire layout

def test_header_layout_is_the_documented_one():
    parts = one(B.numeric(2, 0, 0x0000, 4, [1, 2300]))
    assert parts == {"ndigits": 2, "weight": 0, "sign": 0, "dscale": 4,
                     "digits": [1, 2300]}
    body = B.numeric(2, 0, 0x0000, 4, [1, 2300])
    assert body[0:2] == b"\x00\x02"       # ndigits
    assert body[2:4] == b"\x00\x00"       # weight
    assert body[4:6] == b"\x00\x00"       # sign
    assert body[6:8] == b"\x00\x04"       # dscale
    assert body[8:10] == b"\x00\x01"
    assert body[10:12] == b"\x08\xfc"     # 2300


def test_base_10000_value_relation_is_documented_by_example():
    # weight applies to the first group: 1 * 10000^1 + 2345 = 12345 with
    # dscale 0.  Verified against PostgreSQL separately.
    parts = one(B.numeric(2, 1, 0, 0, [1, 2345]))
    assert to_text(parts) == "12345"


def test_zero_and_trailing_zero_scale_are_distinct():
    zero = one(B.numeric(0, 0, 0, 0, []))
    zero4 = one(B.numeric(0, 0, 0, 4, []))
    assert zero["dscale"] == 0
    assert zero4["dscale"] == 4
    assert to_text(zero) == "0"
    assert to_text(zero4) == "0.0000"
    assert zero != zero4


def test_negative_zero_sign_is_preserved():
    parts = one(B.numeric(0, 0, 0x4000, 4, []))
    assert parts["sign"] == 1
    assert parts["dscale"] == 4
    # PostgreSQL prints a signed zero without the minus sign (verified).
    assert to_text(parts) == "0.0000"


def test_long_decimal_keeps_more_than_28_digits():
    text = "123456789012345678901234567890.1234567890000"
    parts = decoded_of(text)
    assert parts["dscale"] == 13
    assert parts["digits"] == [12, 3456, 7890, 1234, 5678, 9012, 3456,
                               7890, 1234, 5678, 9000]
    arith = 12 * 10000 ** 7 + 3456 * 10000 ** 6 + 7890 * 10000 ** 5 + \
        1234 * 10000 ** 4 + 5678 * 10000 ** 3 + 9012 * 10000 ** 2 + \
        3456 * 10000 + 7890
    assert arith == 123456789012345678901234567890
    assert to_text(parts) == text


def test_high_precision_beyond_decimal_default_context():
    # 60 significant digits; Python's default Decimal context (28) would
    # silently round this.  The codec never touches Decimal.
    text = "123456789012345678901234567890123456789012345678901234567890"
    assert decoded_of(text)["digits"][0] == 1234
    assert to_text(decoded_of(text)) == text


def test_small_weight_groups():
    parts = decoded_of("0.00001234")
    assert parts["weight"] == -2
    assert parts["digits"] == [1234]
    assert to_text(parts) == "0.00001234"


# ------------------------------------------------------------- specials

@pytest.mark.parametrize("special, code", [
    ("NaN", SIGN_NAN),
    ("Infinity", SIGN_PINF),
    ("-Infinity", SIGN_NINF),
])
def test_specials_roundtrip(special, code):
    parts = one(B.numeric(0, 0, code, 0, []))
    assert parts["sign"] == special
    assert parts["digits"] == []
    assert to_text(parts) == special
    out = decoded_of(special)
    assert out["sign"] == special


def test_specials_must_not_carry_digits():
    with pytest.raises(ValueFormatError, match="must not carry"):
        one(B.numeric(1, 0, SIGN_NAN, 0, [1]))
    with pytest.raises(ValueFormatError, match="does not match"):
        decoded_of({"ndigits": 0, "weight": 0, "sign": "NaN", "dscale": 0,
                    "digits": [1]})
    with pytest.raises(ValueFormatError, match="must not carry"):
        decoded_of({"ndigits": 1, "weight": 0, "sign": "NaN", "dscale": 0,
                    "digits": [1]})


# -------------------------------------------------------- rejection rules

def test_negative_ndigits():
    body = b"\xff\xff" + b"\x00\x00\x00\x00" + b"\x00\x00" + b"\x00\x00"
    with pytest.raises(ValueFormatError, match="negative digit count"):
        one(body)


def test_length_group_count_mismatch():
    with pytest.raises(ValueFormatError, match="requires"):
        one(B.numeric(2, 0, 0, 0, [1]))


def test_digit_group_out_of_range():
    with pytest.raises(ValueFormatError, match="9999"):
        one(B.numeric(1, 0, 0, 0, [10000]))


def test_unknown_sign():
    with pytest.raises(ValueFormatError, match="unknown numeric sign"):
        one(B.numeric(0, 0, 0x1234, 0, []))


@pytest.mark.parametrize("sign", [0x0001, 0x4001, 0x6000, 0x8000, 0xA000,
                                  0xE000])
def test_other_unused_sign_bits_rejected(sign):
    with pytest.raises(ValueFormatError):
        one(B.numeric(0, 0, sign, 0, []))


def test_dscale_above_postgres_limit():
    with pytest.raises(ValueFormatError, match="dscale"):
        one(B.numeric(0, 0, 0, 0x4000, []))


def test_ndigits_mismatch_on_encode():
    with pytest.raises(ValueFormatError, match="does not match"):
        decoded_of({"ndigits": 1, "weight": 0, "sign": 0, "dscale": 2,
                    "digits": [1, 2]})


def test_digits_out_of_range_on_encode():
    with pytest.raises(ValueFormatError, match="9999"):
        decoded_of({"ndigits": 1, "weight": 0, "sign": 0, "dscale": 0,
                    "digits": [10000]})


def test_negative_weight_on_encode_ok_but_out_of_int16_fails():
    decoded_of({"ndigits": 1, "weight": -32768, "sign": 0, "dscale": 0,
                "digits": [1]})
    with pytest.raises(ValueFormatError, match="weight"):
        decoded_of({"ndigits": 1, "weight": 32768, "sign": 0, "dscale": 0,
                    "digits": [1]})


def test_negative_dscale_on_encode():
    with pytest.raises(ValueFormatError, match="dscale"):
        decoded_of({"ndigits": 0, "weight": 0, "sign": 0, "dscale": -1,
                    "digits": []})


def test_bad_sign_on_encode():
    with pytest.raises(ValueFormatError, match="sign"):
        decoded_of({"ndigits": 0, "weight": 0, "sign": 7, "dscale": 0,
                    "digits": []})
    with pytest.raises(ValueFormatError, match="unknown numeric sign"):
        decoded_of({"ndigits": 0, "weight": 0, "sign": "nan", "dscale": 0,
                    "digits": []})


def test_unknown_keys_on_encode():
    with pytest.raises(ValueFormatError, match="unknown keys"):
        decoded_of({"ndigits": 0, "weight": 0, "sign": 0, "dscale": 0,
                    "digits": [], "extra": 1})


# ---------------------------------------------------- float must not leak

@pytest.mark.parametrize("value", [1.5, 0.1, 1e20, float("nan"),
                                   float("inf")])
def test_float_values_rejected_everywhere(value):
    with pytest.raises(ValueFormatError, match="object or exact decimal"):
        decoded_of(value)


def test_numeric_string_must_be_exact_decimal_not_scientific():
    with pytest.raises(ValueFormatError, match="not an exact decimal"):
        decoded_of("1e-100")


def test_numeric_string_derives_groups_from_digits_not_float():
    text = "0.1"  # float(0.1) would give 1000000000000000055511151231...
    parts = decoded_of(text)
    assert parts == {"ndigits": 1, "weight": -1, "sign": 0, "dscale": 1,
                     "digits": [1000]}


def test_numeric_string_with_trailing_zeros_autofills_dscale():
    parts = decoded_of("1.2300")
    assert parts["dscale"] == 4
    assert parts["digits"] == [1, 2300]


def test_numeric_string_negative_sign():
    parts = decoded_of("-0.50")
    assert parts["sign"] == 1
    assert parts["dscale"] == 2
    assert parts["digits"] == [5000]


def test_numeric_string_canonical_layout_of_zero_groups():
    # Leading and trailing zero groups are removed; weight adjusted.
    parts = decoded_of("000001.2300")
    assert parts["digits"] == [1, 2300]
    assert parts["weight"] == 0


def test_numeric_string_with_very_large_integer():
    parts = decoded_of("1" + "0" * 40)
    assert parts["weight"] == 10
    assert parts["ndigits"] == 1
    assert parts["digits"] == [1]  # 10^40 == 1 * 10000^10


def test_to_text_discards_hidden_digits_like_postgresql():
    # A value can legally store groups beyond dscale; PostgreSQL prints
    # only dscale digits (verified: 1.2345 with dscale 1 prints 1.2).
    parts = one(B.numeric(2, 0, 0, 1, [1, 2345]))
    assert to_text(parts) == "1.2"
    assert parts["digits"] == [1, 2345]


def test_no_float_anywhere_in_decode(monkeypatch):
    # Spot-check the module surface: it must not import float helpers.
    import pgcopy_bridge.numeric as mod
    source = open(mod.__file__, "rb").read()
    assert b"float(" not in source
    assert b"Decimal(" not in source
