"""DwC-A (Darwin Core Archive) subset importer for SQLite.

Turns a ZIP containing a namespaced ``meta.xml``, one Occurrence core and
zero or more extensions into queryable, attribution-tracked relational data.
The package performs *no* scientific inference: values stay text, keys match
as decoded strings, and extension rows are grouped rather than wide-joined.
"""

from __future__ import annotations

from .errors import ArchiveDataError, DescriptorError, DwcaError
from .importer import ImportResult, TableCount, import_archive
from .library import DwcaLibrary
from .models import (
    ArchiveSpec,
    CellValue,
    CoreRecord,
    DataRow,
    ExtensionRecord,
    FieldSpec,
    QuarantineRecord,
    TableSpec,
)

__all__ = [
    "ArchiveDataError",
    "ArchiveSpec",
    "CellValue",
    "CoreRecord",
    "DataRow",
    "DescriptorError",
    "DwcaError",
    "DwcaLibrary",
    "ExtensionRecord",
    "FieldSpec",
    "ImportResult",
    "QuarantineRecord",
    "TableCount",
    "TableSpec",
    "import_archive",
]
