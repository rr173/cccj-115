"""Column catalogue, type registry and caller-tunable limits.

The catalogue is a JSON document that names columns in order and declares a
PostgreSQL type for each one.  Types are never inferred from data: a declared
type is the only thing the codecs consult.

Catalogue file::

    {"columns": [
        {"name": "id",    "type": "int8"},
        {"name": "label", "type": "text"},
        {"name": "scores", "type": "numeric[]"}
    ]}

See :mod:`pgcopy_bridge.values` for the codecs and ``pgcopy types`` (or
README.md) for the logical JSON representation of every type.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .errors import DescriptorError



# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TypeSpec:
    """One supported PostgreSQL type.

    ``oid`` is the PostgreSQL type OID stored in array headers for element
    types; it is 0 for non-array types.  ``element`` is the :class:`TypeSpec`
    of the element type for an array, else ``None``.
    """

    name: str
    pg_name: str
    oid: int = 0
    element: "TypeSpec | None" = None
    #: logical JSON representation, used in documentation and errors
    logical: str = ""


INT4 = TypeSpec("int4", "integer", 23, None,
                "JSON integer; exact, outside JSON-float worries")
INT8 = TypeSpec("int8", "bigint", 20, None, "JSON integer (exact, up to 2**63-1)")
BOOL = TypeSpec("bool", "boolean", 16, None, "JSON true / false")
TEXT = TypeSpec("text", "text", 25, None, "JSON string; UTF-8, NUL forbidden")
BYTEA = TypeSpec("bytea", "bytea", 17, None,
                 "JSON string of lowercase hex; '\\x' + hex would be ambiguous")
UUID = TypeSpec(
    "uuid", "uuid", 2950, None,
    "JSON string 8-4-4-4-12 lowercase; PostgreSQL canonical spelling")
NUMERIC = TypeSpec(
    "numeric", "numeric", 1700, None,
    "'{ndigits,weight,sign,scale,digits}' object; NaN/Infinity/-Infinity as "
    "the strings \"NaN\"/\"Infinity\"/\"-Infinity\"")
DATE = TypeSpec("date", "date", 1082, None,
                "JSON integer: days since 2000-01-01; -2147483648 / "
                "2147483647 are -infinity / infinity")
TIMESTAMP = TypeSpec("timestamp", "timestamp", 1114, None,
                     "JSON integer: microseconds since 2000-01-01, no "
                     "time zone; -2**63 / 2**63-1 are -infinity / infinity")
TIMESTAMPTZ = TypeSpec("timestamptz", "timestamptz", 1184, None,
                       "JSON integer: microseconds since 2000-01-01 00:00:00 "
                       "UTC; -2**63 / 2**63-1 are -infinity / infinity")

INT4_ARRAY = TypeSpec("int4[]", "integer[]", 0, INT4, None)
TEXT_ARRAY = TypeSpec("text[]", "text[]", 0, TEXT, None)
NUMERIC_ARRAY = TypeSpec("numeric[]", "numeric[]", 0, NUMERIC, None)

TYPE_REGISTRY: dict[str, TypeSpec] = {
    t.name: t for t in (
        INT4, INT8, BOOL, TEXT, BYTEA, UUID, NUMERIC, DATE, TIMESTAMP,
        TIMESTAMPTZ, INT4_ARRAY, TEXT_ARRAY, NUMERIC_ARRAY,
    )
}

#: Accepted aliases only when they are the *same* PostgreSQL type.
ALIASES: dict[str, str] = {
    "integer": "int4",
    "bigint": "int8",
    "boolean": "bool",
    "character varying": "text",
    "varchar": "text",
    "timestamptz": "timestamptz",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
}

SCALAR_TYPES = frozenset(
    {"int4", "int8", "bool", "text", "bytea", "uuid", "numeric", "date",
     "timestamp", "timestamptz"}
)

ARRAY_TYPES = frozenset({"int4[]", "text[]", "numeric[]"})


def supported_type_names() -> list[str]:
    return sorted(TYPE_REGISTRY)


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    name: str
    type: TypeSpec
    index: int  # 0-based position

    @property
    def pg_type(self) -> str:
        return self.type.pg_name


@dataclass(frozen=True)
class Catalogue:
    columns: tuple[Column, ...]
    source: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def __len__(self) -> int:
        return len(self.columns)

    def display(self) -> str:
        return ", ".join(f"{c.name}:{c.type.name}" for c in self.columns)


def normalise_type(raw: str) -> str:
    """Map a declared type spelling to a registry name, or reject it."""
    if not isinstance(raw, str):
        raise DescriptorError(f"type must be a string, got {raw!r}")
    key = " ".join(raw.strip().lower().split())
    key = key.replace(" [ ]", "[]").replace("[ ]", "[]")
    key = key.replace("int4 array", "int4[]")
    if key in TYPE_REGISTRY:
        return key
    if key in ALIASES and ALIASES[key] in TYPE_REGISTRY:
        return ALIASES[key]
    if key.endswith("[]"):
        base = key[:-2].strip()
        base = ALIASES.get(base, base)
        candidate = base + "[]"
        if candidate in TYPE_REGISTRY:
            return candidate
    raise DescriptorError(
        f"unsupported column type {raw!r}; supported types: "
        + ", ".join(supported_type_names()))


def catalogue_from_mapping(doc: object, *, source: str | None = None) -> Catalogue:
    """Validate a parsed catalogue document."""
    if isinstance(doc, list):
        # Bare list of column objects is also accepted.
        doc = {"columns": doc}
    if not isinstance(doc, dict):
        raise DescriptorError("catalogue must be a JSON object with a 'columns' list")
    cols = doc.get("columns")
    if not isinstance(cols, list):
        raise DescriptorError("catalogue 'columns' must be a list")
    if not cols:
        raise DescriptorError("catalogue must declare at least one column")
    seen: dict[str, int] = {}
    out: list[Column] = []
    for i, entry in enumerate(cols):
        where = f"column {i + 1}"
        if not isinstance(entry, dict):
            raise DescriptorError(f"{where}: expected an object with name/type")
        unexpected = set(entry) - {"name", "type", "description", "comment"}
        if unexpected:
            raise DescriptorError(
                f"{where}: unknown keys {sorted(unexpected)}; only name/type allowed")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise DescriptorError(f"{where}: 'name' must be a non-empty string")
        if name in seen:
            raise DescriptorError(
                f"{where}: duplicate column name {name!r} "
                f"(first declared as column {seen[name]})")
        seen[name] = i + 1
        raw_type = entry.get("type")
        if raw_type is None:
            raise DescriptorError(f"{where}: 'type' is required for {name!r}")
        try:
            canonical = normalise_type(raw_type)
        except DescriptorError as exc:
            raise DescriptorError(f"{where} ({name}): {exc.message}") from None
        out.append(Column(name=name, type=TYPE_REGISTRY[canonical], index=i))
    return Catalogue(columns=tuple(out), source=source)


def load_catalogue(path: str | os.PathLike[str]) -> Catalogue:
    """Read and validate a catalogue JSON file."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise DescriptorError(f"cannot read catalogue {path}: {exc}") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DescriptorError(f"catalogue is not UTF-8: {exc}") from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DescriptorError(
            f"catalogue is not valid JSON: {exc.msg} at line {exc.lineno} "
            f"column {exc.colno}") from None
    return catalogue_from_mapping(doc, source=str(path))
