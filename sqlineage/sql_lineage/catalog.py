"""JSON data catalog: case-sensitive registry of relations and columns.

Catalog format::

    {
      "relations": [
        {
          "schema": "sales",
          "table": "orders",
          "columns": ["id", "amount", "cust_id"],
          "description": "optional"
        }
      ]
    }

Schema, table and column names are compared **case-sensitively**, exactly
as written in the JSON and exactly as the identifiers are spelled in SQL
(``"Id"`` and ``id`` are different names).  Duplicate schema/table/column
entries and malformed catalog documents are rejected eagerly.
"""

import json

from .errors import (
    AmbiguousNameError, CatalogError, DuplicateNameError, UnknownNameError,
)


class Catalog:
    def __init__(self, relations):
        # (schema, table) -> list of column names (ordered)
        self.tables = {}
        # table name -> set of (schema, table) keys
        self.by_name = {}
        for rel in relations:
            key = (rel["schema"], rel["table"])
            if key in self.tables:
                raise DuplicateNameError(
                    f"duplicate relation {rel['schema']}.{rel['table']} "
                    "in catalog")
            columns = rel["columns"]
            if not isinstance(columns, list) or not columns:
                raise CatalogError(
                    f"relation {rel['schema']}.{rel['table']} must declare "
                    "a non-empty column list")
            seen = set()
            for col in columns:
                if not isinstance(col, str) or not col:
                    raise CatalogError(
                        f"invalid column name {col!r} in "
                        f"{rel['schema']}.{rel['table']}")
                if col in seen:
                    raise DuplicateNameError(
                        f"duplicate column {col!r} in "
                        f"{rel['schema']}.{rel['table']}")
                seen.add(col)
            self.tables[key] = list(columns)
            self.by_name.setdefault(rel["table"], set()).add(key)

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except OSError as exc:
            raise CatalogError(f"cannot read catalog {path}: {exc}")
        except json.JSONDecodeError as exc:
            raise CatalogError(
                f"invalid JSON in catalog {path}: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})")
        if not isinstance(doc, dict) or not isinstance(
                doc.get("relations"), list):
            raise CatalogError(
                "catalog document must be an object with a 'relations' "
                "array")
        cleaned = []
        for rel in doc["relations"]:
            if not isinstance(rel, dict) or not isinstance(
                    rel.get("schema"), str) or not isinstance(
                    rel.get("table"), str):
                raise CatalogError(
                    "each relation needs string 'schema' and 'table' fields")
            cleaned.append(rel)
        return cls(cleaned)

    def resolve_qualified(self, schema, table):
        """Resolve an explicit schema.table reference (exact match)."""
        key = (schema, table)
        if key not in self.tables:
            raise UnknownNameError(
                f"relation {schema}.{table} is not registered in the "
                "catalog")
        return key

    def resolve_unqualified(self, table):
        """Resolve a bare table name; zero or several matches are errors."""
        matches = self.by_name.get(table, set())
        if not matches:
            raise UnknownNameError(
                f"relation {table!r} is not registered in the catalog")
        if len(matches) > 1:
            ordered = ", ".join(f"{s}.{t}" for s, t in sorted(matches))
            raise AmbiguousNameError(
                f"unqualified relation {table!r} is ambiguous; matches: "
                f"{ordered}")
        return next(iter(matches))

    def columns_of(self, key):
        return list(self.tables[key])

    def has_column(self, key, column):
        return column in self.tables[key]

    def relation_names(self):
        return set(self.tables)
