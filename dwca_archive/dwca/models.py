"""Typed descriptors and read models.

The descriptor classes mirror the *supported DwC-A subset* exactly: only what
``meta.xml`` may declare is represented here.  Read models are what the
:class:`~dwca.library.DwcaLibrary` query API hands back; they never widen
extension rows into Cartesian-product joins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Quarantine reason codes.  They are stored verbatim in the database and form
# part of the public API -- do not rename casually.
# ---------------------------------------------------------------------------

# Core row reasons
REASON_DUPLICATE_KEY = "duplicate_key"   # non-empty id occurs on >1 core rows
REASON_SHORT_ROW = "short_row"           # a mapped column index is out of range

# Extension row reasons (reference handling), checked in this order
REASON_EMPTY_REF = "empty_reference"        # coreid decoded to an empty string
REASON_MISSING_REF = "missing_reference"    # no core row carries this coreid
REASON_AMBIGUOUS_REF = "ambiguous_reference"  # coreid matches duplicate-key rows
REASON_BAD_CORE_REF = "bad_core_reference"  # unique match, but that core row is bad

# Where a cell value came from.  Everything stays text.
ORIGIN_SOURCE = "source"    # an actual cell of the source file
ORIGIN_DEFAULT = "default"  # declared <field default="..."> applied
ORIGIN_EMPTY = "empty"      # empty source cell with no declared default


@dataclass(frozen=True)
class FieldSpec:
    """One ``<field>`` (or core ``<id>``) declaration.

    ``index is None`` means a default-only field: ``default`` must then be
    present.  ``term`` is the full, namespaced URI, never shortened.
    """

    term: str
    index: Optional[int]
    default: Optional[str]
    is_id: bool = False


@dataclass(frozen=True)
class TableSpec:
    """Parsed descriptor of one core or extension table."""

    table_id: str                 # stable identity: "core" or "ext:<rowType URI>"
    row_type: str                 # full rowType URI
    member: str                   # files/location exactly as declared
    encoding: str                 # normalized python codec name
    fields_terminated_by: str     # decoded one-character delimiter
    lines_terminated_by: str      # decoded line terminator (documentation only)
    fields_enclosed_by: str       # quote character ("" disables quoting)
    ignore_header_lines: int
    id_field: Optional[FieldSpec]      # core only
    coreid_field: Optional[FieldSpec]  # extensions only
    fields: tuple[FieldSpec, ...]      # ordered <field> declarations

    @property
    def is_core(self) -> bool:
        return self.table_id == "core"

    @property
    def max_index(self) -> int:
        """Highest physical column index any mapped term points at."""
        indexes = [f.index for f in self.iter_indexed()]
        return max(indexes) if indexes else -1

    def iter_indexed(self):
        if self.id_field is not None and self.id_field.index is not None:
            yield self.id_field
        if self.coreid_field is not None and self.coreid_field.index is not None:
            yield self.coreid_field
        for f in self.fields:
            if f.index is not None:
                yield f


@dataclass(frozen=True)
class ArchiveSpec:
    """The fully validated descriptor of one archive."""

    core: TableSpec
    extensions: tuple[TableSpec, ...]
    members: frozenset[str]          # every member physically present in the ZIP
    xml_bytes: bytes                 # raw meta.xml, stored for provenance

    def all_tables(self) -> tuple[TableSpec, ...]:
        return (self.core,) + self.extensions


@dataclass
class CellValue:
    """One parsed cell: text plus whether the file or a default supplied it."""

    value: str
    origin: str  # one of the ORIGIN_* constants


@dataclass
class DataRow:
    """Common identity carried by every parsed row.

    ``record_number`` is the 1-based *logical* data record number: header
    lines declared by ``ignoreHeaderLines`` are skipped first, and a quoted
    field containing a line break still counts as one record.
    """

    table_id: str
    member: str
    record_number: int
    cells: dict[str, CellValue] = field(default_factory=dict)
    raw_cells: list[str] = field(default_factory=list)
    row_key: Optional[str] = None
    is_short: bool = False


@dataclass
class CoreRecord:
    """One valid core row as returned by the read API."""

    table_id: str
    member: str
    record_number: int
    core_id: str
    values: dict[str, str]
    origins: dict[str, str]
    extensions: dict[str, list["ExtensionRecord"]]

    def get(self, term: str) -> Optional[str]:
        return self.values.get(term)


@dataclass
class ExtensionRecord:
    """One valid, attributed extension row."""

    table_id: str
    member: str
    record_number: int
    core_id: str
    values: dict[str, str]
    origins: dict[str, str]


@dataclass
class QuarantineRecord:
    """One isolated row with every reason that applied to it."""

    table_id: str
    member: str
    record_number: int
    key_value: Optional[str]        # core id or extension coreid, if decodable
    reasons: list[str]
    values: dict[str, str]
    origins: dict[str, str]
    raw_cells: list[str]
