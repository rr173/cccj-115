"""PostgreSQL ``numeric`` binary codec -- exact, never via floats.

Wire layout (all integers network byte order, header is 8 bytes)::

    int16  ndigits    number of base-10000 digit groups that follow
    int16  weight     weight of the first group; value = sum(digit[i] *
                      10000 ** (weight - i))
    uint16 sign       0x0000 positive, 0x4000 negative, 0xC000 NaN,
                      0xD000 +Infinity, 0xF000 -Infinity
    uint16 dscale     number of decimal digits to display after the point
    uint16 digits[ndigits]   each 0 .. 9999

The codec keeps every one of those fields.  ``0.0000`` is therefore not
``0``: it is ``ndigits=0, weight=0, sign=0, dscale=4, digits=[]``.

The logical JSON representation is an object::

    {"ndigits":2,"weight":0,"sign":0,"dscale":4,"digits":[1,2300]}

``sign`` is ``0``/``1`` for finite values and one of the strings ``"NaN"``,
``"Infinity"``, ``"-Infinity"`` for the three special values.  A finite
value can also be *written* as an exact decimal string such as
``"-12.3400"``; the encoder derives the groups from the digits of that
string only.  JSON floating-point literals are never accepted anywhere in
the input, so no binary approximation can leak in.

``numeric_to_text`` is provided for humans and materials.  It mirrors
PostgreSQL's own output rule: digits past ``dscale`` are discarded (not
rounded), as PostgreSQL does when it prints such a value.
"""

from __future__ import annotations

import re

from .errors import ValueFormatError

SIGN_POS = 0x0000
SIGN_NEG = 0x4000
SIGN_NAN = 0xC000
SIGN_PINF = 0xD000
SIGN_NINF = 0xF000

_SIGN_TO_CODE = {0: SIGN_POS, 1: SIGN_NEG}
_SPECIAL_TO_CODE = {"NaN": SIGN_NAN, "Infinity": SIGN_PINF,
                    "-Infinity": SIGN_NINF}
_CODE_TO_SPECIAL = {v: k for k, v in _SPECIAL_TO_CODE.items()}

MAX_NDIGITS = 32767
MAX_DSCALE = 0x3FFF  # PostgreSQL NUMERIC_DSCALE_MAX
MIN_WEIGHT = -32768
MAX_WEIGHT = 32767

_DECIMAL_RE = re.compile(r"^([+-]?)([0-9]+)(?:\.([0-9]*))?$")


def _fail(message: str, ctx, rel: int = 0) -> "ValueFormatError":
    base = getattr(ctx, "offset", None)
    offset = None if base is None else base + rel
    return ValueFormatError(
        message,
        offset=offset,
        line=getattr(ctx, "line", None),
        field=getattr(ctx, "field", None),
        field_name=getattr(ctx, "field_name", None),
    )


def _as_int(value, what: str, ctx) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{what} must be a JSON integer, got "
                    f"{type(value).__name__}", ctx)
    return value


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def decode(body: bytes, ctx) -> dict:
    """Decode one ``numeric`` field body to its logical object."""
    if len(body) < 8:
        raise _fail(f"numeric body is {len(body)} bytes, header needs 8", ctx)
    ndigits = int.from_bytes(body[0:2], "big", signed=True)
    weight = int.from_bytes(body[2:4], "big", signed=True)
    sign = int.from_bytes(body[4:6], "big", signed=False)
    dscale = int.from_bytes(body[6:8], "big", signed=False)

    if ndigits < 0:
        raise _fail(f"numeric declares negative digit count {ndigits}", ctx, 0)
    if dscale > MAX_DSCALE:
        raise _fail(
            f"numeric dscale {dscale} exceeds PostgreSQL maximum "
            f"{MAX_DSCALE}", ctx, 6)
    expected = 8 + 2 * ndigits
    if len(body) != expected:
        raise _fail(
            f"numeric body is {len(body)} bytes but ndigits={ndigits} "
            f"requires {expected}", ctx, 0)

    digits = [
        int.from_bytes(body[8 + 2 * i:10 + 2 * i], "big") for i in range(ndigits)
    ]
    for i, digit in enumerate(digits):
        if digit > 9999:
            raise _fail(
                f"numeric digit group {i} is {digit}, allowed range 0..9999",
                ctx, 8 + 2 * i)

    if sign in _CODE_TO_SPECIAL:
        special = _CODE_TO_SPECIAL[sign]
        if ndigits != 0:
            raise _fail(
                f"numeric {special} must not carry digit groups "
                f"(ndigits={ndigits})", ctx, 0)
        return {
            "ndigits": 0,
            "weight": weight,
            "sign": special,
            "dscale": dscale,
            "digits": [],
        }
    if sign not in (SIGN_POS, SIGN_NEG):
        raise _fail(f"unknown numeric sign 0x{sign:04x}", ctx, 4)
    return {
        "ndigits": ndigits,
        "weight": weight,
        "sign": 1 if sign == SIGN_NEG else 0,
        "dscale": dscale,
        "digits": digits,
    }


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

def _parts_from_decimal_string(text: str, ctx) -> dict:
    match = _DECIMAL_RE.match(text)
    if not match:
        raise _fail(
            f"numeric string {text!r} is not an exact decimal: expected "
            f"[+-]digits[.digits], 'NaN', 'Infinity' or '-Infinity'", ctx)
    sign_char, int_part, frac_part = match.groups()
    frac_part = frac_part or ""
    dscale = len(frac_part)
    if dscale > MAX_DSCALE:
        raise _fail(
            f"numeric scale {dscale} exceeds maximum {MAX_DSCALE}", ctx)

    # Build groups aligned on the decimal point.  The integer part is padded
    # on the left, the fraction on the right, exactly like PostgreSQL's
    # base-10000 representation.
    int_groups = (len(int_part) + 3) // 4
    int_padded = int_part.rjust(int_groups * 4, "0")
    frac_padded = frac_part.ljust(((len(frac_part) + 3) // 4) * 4, "0")
    groups = [
        int(int_padded[i:i + 4]) for i in range(0, len(int_padded), 4)
    ] + [int(frac_padded[i:i + 4]) for i in range(0, len(frac_padded), 4)]
    weight = int_groups - 1

    while groups and groups[-1] == 0:
        groups.pop()
    while groups and groups[0] == 0:
        groups.pop(0)
        weight -= 1
    if not groups:
        weight = 0
    if len(groups) > MAX_NDIGITS:
        raise _fail(
            f"numeric needs {len(groups)} digit groups, maximum is "
            f"{MAX_NDIGITS}", ctx)
    if not (MIN_WEIGHT <= weight <= MAX_WEIGHT):
        raise _fail(
            f"numeric weight {weight} outside int16 range "
            f"{MIN_WEIGHT}..{MAX_WEIGHT}; value is out of PostgreSQL's "
            f"numeric range", ctx)
    return {
        "ndigits": len(groups),
        "weight": weight,
        "sign": 1 if sign_char == "-" else 0,
        "dscale": dscale,
        "digits": groups,
    }


def _validate_parts(value: dict, ctx) -> dict:
    allowed = {"ndigits", "weight", "sign", "dscale", "digits"}
    unexpected = set(value) - allowed
    if unexpected:
        raise _fail(
            f"numeric object has unknown keys {sorted(unexpected)}; allowed "
            f"are {sorted(allowed)}", ctx)
    for key in ("ndigits", "weight", "sign", "dscale"):
        if key not in value:
            raise _fail(f"numeric object is missing {key!r}", ctx)
    if "digits" not in value:
        raise _fail("numeric object is missing 'digits'", ctx)

    ndigits = _as_int(value["ndigits"], "numeric ndigits", ctx)
    weight = _as_int(value["weight"], "numeric weight", ctx)
    dscale = _as_int(value["dscale"], "numeric dscale", ctx)
    sign = value["sign"]
    digits = value["digits"]

    if ndigits < 0 or ndigits > MAX_NDIGITS:
        raise _fail(f"numeric ndigits {ndigits} outside 0..{MAX_NDIGITS}", ctx)
    if not (MIN_WEIGHT <= weight <= MAX_WEIGHT):
        raise _fail(f"numeric weight {weight} outside int16 range", ctx)
    if dscale < 0 or dscale > MAX_DSCALE:
        raise _fail(f"numeric dscale {dscale} outside 0..{MAX_DSCALE}", ctx)
    if not isinstance(digits, list):
        raise _fail("numeric digits must be a JSON array", ctx)
    if ndigits != len(digits):
        raise _fail(
            f"numeric ndigits={ndigits} does not match {len(digits)} "
            f"digit groups", ctx)
    clean_digits = []
    for i, digit in enumerate(digits):
        digit = _as_int(digit, f"numeric digit group {i}", ctx)
        if digit < 0 or digit > 9999:
            raise _fail(
                f"numeric digit group {i} is {digit}, allowed range 0..9999",
                ctx)
        clean_digits.append(digit)

    if isinstance(sign, str):
        if sign not in _SPECIAL_TO_CODE:
            raise _fail(f"unknown numeric sign {sign!r}", ctx)
        if ndigits != 0:
            raise _fail(f"numeric {sign} must not carry digit groups", ctx)
        clean_sign = sign
    else:
        clean_sign = _as_int(sign, "numeric sign", ctx)
        if clean_sign not in (0, 1):
            raise _fail(
                f"numeric sign must be 0 or 1 for finite values, got "
                f"{clean_sign}", ctx)
    return {
        "ndigits": ndigits,
        "weight": weight,
        "sign": clean_sign,
        "dscale": dscale,
        "digits": clean_digits,
    }


def encode(value, ctx) -> bytes:
    """Encode a logical numeric (object or exact decimal string)."""
    if isinstance(value, str):
        if value in _SPECIAL_TO_CODE:
            parts = {"ndigits": 0, "weight": 0, "sign": value,
                     "dscale": 0, "digits": []}
        else:
            parts = _parts_from_decimal_string(value, ctx)
    elif isinstance(value, dict):
        parts = _validate_parts(value, ctx)
    else:
        raise _fail(
            f"numeric must be an object or exact decimal string, got "
            f"{type(value).__name__}", ctx)

    if isinstance(parts["sign"], str):
        sign_code = _SPECIAL_TO_CODE[parts["sign"]]
    else:
        sign_code = _SIGN_TO_CODE[parts["sign"]]
    out = bytearray(8 + 2 * parts["ndigits"])
    out[0:2] = parts["ndigits"].to_bytes(2, "big", signed=True)
    out[2:4] = parts["weight"].to_bytes(2, "big", signed=True)
    out[4:6] = sign_code.to_bytes(2, "big", signed=False)
    out[6:8] = parts["dscale"].to_bytes(2, "big", signed=False)
    for i, digit in enumerate(parts["digits"]):
        out[8 + 2 * i:10 + 2 * i] = digit.to_bytes(2, "big")
    return bytes(out)


# --------------------------------------------------------------------------
# Human-readable rendering (not used by the codec)
# --------------------------------------------------------------------------

def to_text(parts: dict) -> str:
    """Render a logical numeric the way PostgreSQL prints it.

    Extra digit groups beyond ``dscale`` are discarded, matching
    PostgreSQL's ``numeric_out``.  This is a display helper only; the codec
    itself never collapses the number.
    """
    special = parts.get("sign")
    if isinstance(special, str):
        return special
    if not parts["digits"]:
        value = "0"
        if parts["dscale"]:
            value += "." + "0" * parts["dscale"]
        # PostgreSQL prints any zero without a minus sign; the sign field is
        # still preserved in the logical object.
        return value

    raw = "".join(f"{d:04d}" for d in parts["digits"])
    point = (parts["weight"] + 1) * 4
    if point <= 0:
        integer = "0"
        fraction = "0" * (-point) + raw
    elif point >= len(raw):
        integer = raw + "0" * (point - len(raw))
        fraction = ""
    else:
        integer = raw[:point]
        fraction = raw[point:]
    integer = integer.lstrip("0") or "0"
    # A finite zero has no minus sign in PostgreSQL's output; the sign field
    # itself is still kept in the logical object.
    negative = bool(parts["sign"]) and any(parts["digits"])
    if parts["dscale"] == 0:
        return ("-" if negative else "") + integer
    fraction = fraction[:parts["dscale"]].ljust(parts["dscale"], "0")
    return ("-" if negative else "") + integer + "." + fraction
