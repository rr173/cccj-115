"""Independent byte-level inspector for encoded COPY files.

Deliberately separate from the production decoder: it re-implements the
wire walk with ``struct`` and checks every protocol field, then returns a
plain nested structure.  Used by tests to prove that the encoder produced a
legal file *before* asking the production decoder to read it back.

It never imports :mod:`pgcopy_bridge`.
"""

from __future__ import annotations

import struct

SIG = b"PGCOPY\n\xff\r\n\x00"


class BadBytes(AssertionError):
    pass


def inspect(data: bytes, column_types: list[str]) -> dict:
    """Walk a binary COPY file and check structure and every scalar body.

    ``column_types`` are the catalogue spellings (``int4``, ``numeric[]``...).
    Returns ``{"flags", "extension_length", "tuples": [[field...]]}`` where
    each field is either ``None`` or ``{"body": bytes, ...parsed extras}``.
    """
    if not data.startswith(SIG):
        raise BadBytes(f"missing signature: starts with {data[:11]!r}")
    pos = 11
    flags, ext = struct.unpack_from(">ii", data, pos)
    pos += 8
    if flags != 0:
        raise BadBytes(f"flags {flags} != 0")
    if ext < 0 or pos + ext > len(data):
        raise BadBytes(f"bad extension length {ext}")
    extension = data[pos:pos + ext]
    pos += ext

    tuples = []
    while True:
        if pos + 2 > len(data):
            raise BadBytes(f"missing end marker; EOF at {pos}")
        (count,) = struct.unpack_from(">h", data, pos)
        pos += 2
        if count == -1:
            break
        if count < 0:
            raise BadBytes(f"negative tuple field count {count} at {pos - 2}")
        if count != len(column_types):
            raise BadBytes(f"tuple field count {count} != {len(column_types)}")
        fields = []
        for index, type_name in enumerate(column_types):
            if pos + 4 > len(data):
                raise BadBytes(f"truncated length at {pos}")
            (length,) = struct.unpack_from(">i", data, pos)
            pos += 4
            if length == -1:
                fields.append(None)
                continue
            if length < -1:
                raise BadBytes(f"field length {length} < -1 at {pos - 4}")
            if pos + length > len(data):
                raise BadBytes(
                    f"field {index} body ({length} bytes) overruns at {pos}")
            body = data[pos:pos + length]
            pos += length
            fields.append(_check(type_name, body, index, pos - length))
        tuples.append(fields)
    if pos != len(data):
        raise BadBytes(f"{len(data) - pos} trailing byte(s) after end marker")
    return {"flags": flags, "extension_length": ext,
            "extension": extension, "tuples": tuples}


def _check(type_name, body: bytes, index: int, offset: int) -> dict:
    name = type_name[:-2] if type_name.endswith("[]") else type_name
    if type_name.endswith("[]"):
        return _check_array(body, name, index, offset)
    return _check_scalar(name, body, index, offset)


def _fixed(body, size, index, name):
    if len(body) != size:
        raise BadBytes(f"field {index} ({name}) body {len(body)} != {size}")
    return int.from_bytes(body, "big", signed=True)


def _check_scalar(name, body, index, offset):
    if name in ("int4", "date"):
        return {"kind": name, "value": _fixed(body, 4, index, name)}
    if name in ("int8", "timestamp", "timestamptz"):
        return {"kind": name, "value": _fixed(body, 8, index, name)}
    if name == "bool":
        if len(body) != 1:
            raise BadBytes(f"field {index} (bool) body {len(body)} != 1")
        if body[0] not in (0, 1):
            raise BadBytes(f"field {index} (bool) body byte {body[0]}")
        return {"kind": "bool", "value": bool(body[0])}
    if name == "bytea":
        return {"kind": "bytea", "value": body}
    if name == "text":
        if b"\x00" in body:
            raise BadBytes(f"field {index} (text) contains NUL")
        return {"kind": "text", "value": body.decode("utf-8")}
    if name == "uuid":
        if len(body) != 16:
            raise BadBytes(f"field {index} (uuid) body {len(body)} != 16")
        return {"kind": "uuid", "value": body.hex()}
    if name == "numeric":
        return {"kind": "numeric", "value": _check_numeric(body, index)}
    raise BadBytes(f"unknown type {name}")


def _check_numeric(body, index):
    if len(body) < 8:
        raise BadBytes(f"numeric body {len(body)} < 8")
    ndigits, weight, sign, dscale = struct.unpack_from(">hhHH", body, 0)
    if ndigits < 0:
        raise BadBytes(f"numeric ndigits {ndigits} < 0")
    if len(body) != 8 + 2 * ndigits:
        raise BadBytes(
            f"numeric body {len(body)} != 8 + 2*{ndigits}")
    if dscale > 0x3FFF:
        raise BadBytes(f"numeric dscale {dscale} > 16383")
    digits = [struct.unpack_from(">H", body, 8 + 2 * i)[0]
              for i in range(ndigits)]
    for i, digit in enumerate(digits):
        if digit > 9999:
            raise BadBytes(f"numeric group {i} = {digit} > 9999")
    if sign not in (0x0000, 0x4000, 0xC000, 0xD000, 0xF000):
        raise BadBytes(f"numeric sign 0x{sign:04x} unknown")
    if sign in (0xC000, 0xD000, 0xF000) and ndigits:
        raise BadBytes("numeric special carries digit groups")
    return {"ndigits": ndigits, "weight": weight, "sign": sign,
            "dscale": dscale, "digits": digits}


OIDS = {"int4": 23, "text": 25, "numeric": 1700}


def _check_array(body, element, index, offset):
    if len(body) < 12:
        raise BadBytes(f"array body {len(body)} < 12")
    ndim, has_null, oid = struct.unpack_from(">iii", body, 0)
    if ndim < 0 or ndim > 6:
        raise BadBytes(f"array ndim {ndim}")
    if oid != OIDS[element]:
        raise BadBytes(f"array element oid {oid} != {OIDS[element]}")
    if has_null not in (0, 1):
        raise BadBytes(f"array NULL flag {has_null}")
    if len(body) < 12 + 8 * ndim:
        raise BadBytes("array dimension block truncated")
    dims = []
    total = 1 if ndim else 0
    for i in range(ndim):
        length, lower = struct.unpack_from(">ii", body, 12 + 8 * i)
        if length < 0:
            raise BadBytes(f"array dimension {i} negative length {length}")
        dims.append((length, lower))
        total *= length
    pos = 12 + 8 * ndim
    elements = []
    for i in range(total):
        if pos + 4 > len(body):
            raise BadBytes(f"array element {i} length truncated")
        (length,) = struct.unpack_from(">i", body, pos)
        pos += 4
        if length == -1:
            elements.append(None)
            continue
        if length < -1:
            raise BadBytes(f"array element {i} length {length} < -1")
        payload = body[pos:pos + length]
        if len(payload) != length:
            raise BadBytes(f"array element {i} body truncated")
        pos += length
        if element == "int4" and length != 4:
            raise BadBytes(f"array element {i} int4 body {length} != 4")
        if element == "numeric":
            _check_numeric(payload, i)
        elements.append(payload if element != "int4" else int(
            struct.unpack_from(">i", payload)[0]))
    if pos != len(body):
        raise BadBytes(f"array has {len(body) - pos} trailing byte(s)")
    nulls = sum(1 for e in elements if e is None)
    if has_null and not nulls:
        raise BadBytes("array NULL flag 1 but no NULL element")
    if nulls and not has_null:
        raise BadBytes("array NULL element but NULL flag 0")
    return {"kind": "array", "dims": dims, "elements": elements,
            "element_type": element}
