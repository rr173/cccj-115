"""Scalar value codecs and the array wrapper.

Logical JSON representation per type (also printed by ``pgcopy types``):

=================  =====================================================
type               JSON value
=================  =====================================================
int4, int8         integer (arbitrary precision in Python, exact)
bool               true / false
text               string (UTF-8, NUL rejected)
bytea              string, lowercase hex, two chars per byte ("" = empty)
uuid               string, canonical 8-4-4-4-12 lowercase
numeric            object as documented in :mod:`pgcopy_bridge.numeric`
date               integer days since 2000-01-01
timestamp          integer microseconds since 2000-01-01 00:00:00
timestamptz        integer microseconds since 2000-01-01 00:00:00+00
int4[] etc.        {"shape": [...], "lower_bounds": [...], "elements": [...]}
=================  =====================================================

NULL is JSON ``null`` in the outer record.  Empty text is ``""``, empty
bytea is ``""``, an empty array is ``{"shape": [], "lower_bounds": [],
"elements": []}``.  These are four distinct spellings and none of them is
``null``.

An array element may itself be ``null``; that is a NULL element and differs
from ``""``.
"""

from __future__ import annotations

import uuid as uuid_module

from . import numeric as numeric_module
from .errors import CopyFormatError, ValueFormatError
from .limits import Limits

__all__ = [
    "SCALARS",
    "decode_scalar",
    "encode_scalar",
    "decode_array",
    "encode_array",
    "ARRAY_HEADER_BYTES",
]

# date / timestamp sentinels, verified against PostgreSQL 16
DATE_NEG_INFINITY = -(2 ** 31)
DATE_POS_INFINITY = 2 ** 31 - 1
TS_NEG_INFINITY = -(2 ** 63)
TS_POS_INFINITY = 2 ** 63 - 1

INT4_MIN, INT4_MAX = -(2 ** 31), 2 ** 31 - 1
INT8_MIN, INT8_MAX = -(2 ** 63), 2 ** 63 - 1


def _err(message: str, ctx, rel: int = 0) -> ValueFormatError:
    base = getattr(ctx, "offset", None)
    return ValueFormatError(
        message,
        offset=None if base is None else base + rel,
        line=getattr(ctx, "line", None),
        field=getattr(ctx, "field", None),
        field_name=getattr(ctx, "field_name", None),
    )


def _exact_int(value, what: str, ctx) -> int:
    # bool is an int subclass; never let true/false become 1/0.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(f"{what} must be a JSON integer, got {type(value).__name__}",
                   ctx)
    return value


# --------------------------------------------------------------------------
# Scalar decoders
# --------------------------------------------------------------------------

def _decode_int4(body: bytes, ctx):
    if len(body) != 4:
        raise _err(f"int4 body must be 4 bytes, got {len(body)}", ctx)
    return int.from_bytes(body, "big", signed=True)


def _decode_int8(body: bytes, ctx):
    if len(body) != 8:
        raise _err(f"int8 body must be 8 bytes, got {len(body)}", ctx)
    return int.from_bytes(body, "big", signed=True)


def _decode_bool(body: bytes, ctx):
    if len(body) != 1:
        raise _err(f"bool body must be 1 byte, got {len(body)}", ctx)
    if body[0] not in (0, 1):
        raise _err(f"bool body must be 0 or 1, got {body[0]}", ctx)
    return body[0] == 1


def _decode_text(body: bytes, ctx):
    if b"\x00" in body:
        raise _err("text contains a NUL byte, PostgreSQL rejects it", ctx,
                   body.index(0))
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _err(f"text is not valid UTF-8: {exc.reason} at byte "
                   f"{exc.start}", ctx, exc.start) from None


def _decode_bytea(body: bytes, ctx):
    return body.hex()


def _decode_uuid(body: bytes, ctx):
    if len(body) != 16:
        raise _err(f"uuid body must be 16 bytes, got {len(body)}", ctx)
    return str(uuid_module.UUID(bytes=body))


def _decode_numeric(body: bytes, ctx):
    return numeric_module.decode(body, ctx)


def _decode_date(body: bytes, ctx):
    if len(body) != 4:
        raise _err(f"date body must be 4 bytes, got {len(body)}", ctx)
    return int.from_bytes(body, "big", signed=True)


def _decode_timestamp(body: bytes, ctx):
    if len(body) != 8:
        raise _err(f"timestamp body must be 8 bytes, got {len(body)}", ctx)
    return int.from_bytes(body, "big", signed=True)


SCALARS = {
    "int4": _decode_int4,
    "int8": _decode_int8,
    "bool": _decode_bool,
    "text": _decode_text,
    "bytea": _decode_bytea,
    "uuid": _decode_uuid,
    "numeric": _decode_numeric,
    "date": _decode_date,
    "timestamp": _decode_timestamp,
    "timestamptz": _decode_timestamp,
}


def decode_scalar(type_name: str, body: bytes, ctx):
    return SCALARS[type_name](body, ctx)


# --------------------------------------------------------------------------
# Scalar encoders
# --------------------------------------------------------------------------

def _encode_int4(value, ctx):
    value = _exact_int(value, "int4", ctx)
    if not INT4_MIN <= value <= INT4_MAX:
        raise _err(f"int4 value {value} outside {INT4_MIN}..{INT4_MAX}", ctx)
    return value.to_bytes(4, "big", signed=True)


def _encode_int8(value, ctx):
    value = _exact_int(value, "int8", ctx)
    if not INT8_MIN <= value <= INT8_MAX:
        raise _err(f"int8 value {value} outside {INT8_MIN}..{INT8_MAX}", ctx)
    return value.to_bytes(8, "big", signed=True)


def _encode_bool(value, ctx):
    if not isinstance(value, bool):
        raise _err(f"bool must be true or false, got {type(value).__name__}",
                   ctx)
    return b"\x01" if value else b"\x00"


def _encode_text(value, ctx):
    if not isinstance(value, str):
        raise _err(f"text must be a JSON string, got {type(value).__name__}",
                   ctx)
    try:
        body = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _err(f"text is not encodable as UTF-8: {exc.reason}", ctx) from None
    if b"\x00" in body:
        raise _err("text contains a NUL byte, PostgreSQL rejects it", ctx)
    return body


def _encode_bytea(value, ctx):
    if not isinstance(value, str):
        raise _err(f"bytea must be a hex string, got {type(value).__name__}",
                   ctx)
    if len(value) % 2:
        raise _err(f"bytea hex string has odd length {len(value)}", ctx)
    try:
        return bytes.fromhex(value)
    except ValueError:
        raise _err("bytea is not valid hex", ctx) from None


def _encode_uuid(value, ctx):
    if not isinstance(value, str):
        raise _err(f"uuid must be a string, got {type(value).__name__}", ctx)
    try:
        return uuid_module.UUID(value).bytes
    except (ValueError, AttributeError, TypeError):
        raise _err(f"uuid {value!r} is not a valid UUID", ctx) from None


def _encode_numeric(value, ctx):
    return numeric_module.encode(value, ctx)


def _encode_date(value, ctx):
    value = _exact_int(value, "date", ctx)
    if not INT4_MIN <= value <= INT4_MAX:
        raise _err(f"date day count {value} outside int32", ctx)
    return value.to_bytes(4, "big", signed=True)


def _encode_timestamp(value, ctx):
    value = _exact_int(value, "timestamp", ctx)
    if not INT8_MIN <= value <= INT8_MAX:
        raise _err(f"timestamp microsecond count {value} outside int64", ctx)
    return value.to_bytes(8, "big", signed=True)


ENCODERS = {
    "int4": _encode_int4,
    "int8": _encode_int8,
    "bool": _encode_bool,
    "text": _encode_text,
    "bytea": _encode_bytea,
    "uuid": _encode_uuid,
    "numeric": _encode_numeric,
    "date": _encode_date,
    "timestamp": _encode_timestamp,
    "timestamptz": _encode_timestamp,
}


def encode_scalar(type_name: str, value, ctx) -> bytes:
    return ENCODERS[type_name](value, ctx)


# --------------------------------------------------------------------------
# Arrays
# --------------------------------------------------------------------------

ARRAY_HEADER_BYTES = 12


def _int32(text: str, what: str, ctx) -> int:
    if isinstance(text, bool) or not isinstance(text, int):
        raise _err(f"{what} must be a JSON integer, got {type(text).__name__}",
                   ctx)
    if not -(2 ** 31) <= text <= 2 ** 31 - 1:
        raise _err(f"{what} {text} outside int32", ctx)
    return text


def decode_array(element_type: str, element_oid: int, body: bytes,
                 limits: Limits, ctx) -> dict:
    """Decode an array field body to ``{shape, lower_bounds, elements}``."""

    if len(body) < ARRAY_HEADER_BYTES:
        raise _err(
            f"array body is {len(body)} bytes, header needs "
            f"{ARRAY_HEADER_BYTES}", ctx)
    ndim = int.from_bytes(body[0:4], "big", signed=True)
    has_nulls = int.from_bytes(body[4:8], "big", signed=True)
    oid = int.from_bytes(body[8:12], "big", signed=True)
    limits.check_array_dims(ndim, offset=ctx.offset, line=ctx.line,
                            field_index=ctx.field,
                            field_name=ctx.field_name)
    if has_nulls not in (0, 1):
        raise _err(f"array NULL flag must be 0 or 1, got {has_nulls}", ctx, 4)
    if oid != element_oid:
        raise _err(
            f"array element OID {oid} does not match catalogue element type "
            f"OID {element_oid}", ctx, 8)

    header_end = ARRAY_HEADER_BYTES + 8 * ndim
    if len(body) < header_end:
        raise _err(
            f"array declares {ndim} dimension(s) but body has "
            f"{len(body)} bytes, header needs {header_end}", ctx)

    shape: list[int] = []
    lower_bounds: list[int] = []
    # A zero-dimensional array is the empty array: it has no elements.
    total = 1 if ndim else 0
    for i in range(ndim):
        rel = ARRAY_HEADER_BYTES + 8 * i
        length = int.from_bytes(body[rel:rel + 4], "big", signed=True)
        lower = int.from_bytes(body[rel + 4:rel + 8], "big", signed=True)
        if length < 0:
            raise _err(f"array dimension {i} length {length} is negative", ctx,
                       rel)
        shape.append(length)
        lower_bounds.append(lower)
        total *= length
        limits.check_array_elements(total, offset=ctx.offset, line=ctx.line,
                                    field_index=ctx.field,
                                    field_name=ctx.field_name)

    # Iterate over the slice directly: an array body is already bounded by
    # max_field_bytes, so a second buffering layer would only add copies.
    elements: list = []
    pos = header_end
    end = len(body)
    for index in range(total):
        if pos + 4 > end:
            raise CopyFormatError(
                f"truncated input: array element {index} length needs 4 "
                f"byte(s), only {end - pos} available",
                offset=ctx.offset + pos, line=ctx.line, field=ctx.field,
                field_name=ctx.field_name)
        selem = int.from_bytes(body[pos:pos + 4], "big", signed=True)
        pos += 4
        if selem == -1:
            elements.append(None)
            continue
        if selem < -1:
            raise _err(f"array element {index} has length {selem} < -1",
                       _ElemCtx(ctx, pos - 4))
        limits.check_field_bytes(selem, offset=ctx.offset + pos - 4,
                                 line=ctx.line, field_index=ctx.field,
                                 field_name=ctx.field_name)
        if pos + selem > end:
            raise CopyFormatError(
                f"truncated input: array element {index} body needs "
                f"{selem} byte(s), only {end - pos} available",
                offset=ctx.offset + pos, line=ctx.line, field=ctx.field,
                field_name=ctx.field_name)
        payload = body[pos:pos + selem]
        pos += selem
        elements.append(decode_scalar(element_type, payload,
                                      _ElemCtx(ctx, pos - selem)))

    if pos != end:
        raise _err(
            f"array has {total} element(s) per shape but {end - pos} "
            f"trailing byte(s) remain after the last element",
            ctx, pos)
    null_count = sum(1 for e in elements if e is None)
    if has_nulls and not null_count:
        raise _err(
            "array NULL flag is 1 but no NULL elements are present", ctx, 4)
    if has_nulls == 0 and null_count:
        raise _err(
            f"array NULL flag is 0 but {null_count} NULL element(s) are "
            f"present", ctx, 4)
    return {"shape": shape, "lower_bounds": lower_bounds,
            "elements": elements}


def encode_array(element_type: str, element_oid: int, value, limits: Limits,
                 ctx) -> bytes:
    """Encode ``{shape, lower_bounds, elements}`` to an array field body."""
    if not isinstance(value, dict):
        raise _err(
            "array must be a JSON object with shape, lower_bounds and "
            "elements", ctx)
    allowed = {"shape", "lower_bounds", "elements"}
    unexpected = set(value) - allowed
    if unexpected:
        raise _err(f"array object has unknown keys {sorted(unexpected)}", ctx)
    for key in allowed:
        if key not in value:
            raise _err(f"array object is missing {key!r}", ctx)
    shape = value["shape"]
    lower_bounds = value["lower_bounds"]
    elements = value["elements"]
    if not isinstance(shape, list) or not isinstance(lower_bounds, list):
        raise _err("array shape and lower_bounds must be JSON arrays", ctx)
    if not isinstance(elements, list):
        raise _err("array elements must be a JSON array", ctx)
    if len(shape) != len(lower_bounds):
        raise _err(
            f"array shape has {len(shape)} dimension(s) but lower_bounds "
            f"has {len(lower_bounds)}", ctx)

    ndim = len(shape)
    limits.check_array_dims(ndim, offset=ctx.offset, line=ctx.line,
                            field_index=ctx.field,
                            field_name=ctx.field_name)
    total = 1 if ndim else 0
    for i, length in enumerate(shape):
        length = _int32(length, f"array shape[{i}]", ctx)
        if length < 0:
            raise _err(f"array shape[{i}] is negative ({length})", ctx)
        total *= length
        limits.check_array_elements(total, offset=ctx.offset, line=ctx.line,
                                    field_index=ctx.field,
                                    field_name=ctx.field_name)
    for i, lower in enumerate(lower_bounds):
        _int32(lower, f"array lower_bounds[{i}]", ctx)
    if len(elements) != total:
        raise _err(
            f"array declares {total} element(s) (product of shape {shape}) "
            f"but provides {len(elements)}", ctx)

    out = bytearray(ARRAY_HEADER_BYTES + 8 * ndim)
    out[0:4] = ndim.to_bytes(4, "big", signed=True)
    # The NULL flag needs the element bodies; fill it in after the loop.
    out[8:12] = element_oid.to_bytes(4, "big", signed=True)
    for i, length in enumerate(shape):
        rel = ARRAY_HEADER_BYTES + 8 * i
        out[rel:rel + 4] = length.to_bytes(4, "big", signed=True)
        out[rel + 4:rel + 8] = lower_bounds[i].to_bytes(4, "big", signed=True)

    has_null = 0
    for index, element in enumerate(elements):
        if element is None:
            has_null = 1
            out += b"\xff\xff\xff\xff"
            continue
        body = encode_scalar(element_type, element, ctx)
        out += len(body).to_bytes(4, "big", signed=True)
        out += body
    out[4:8] = has_null.to_bytes(4, "big", signed=True)
    return bytes(out)


class _ElemCtx:
    __slots__ = ("offset", "line", "field", "field_name")

    def __init__(self, parent, offset: int) -> None:
        self.offset = offset
        self.line = parent.line
        self.field = parent.field
        self.field_name = parent.field_name
