"""Synthetic DwC-A archives for tests.

Everything here is assembled from hand-written bytes and an independent
``meta.xml`` writer -- the production descriptor parser is never used to
derive expectations.  Each builder returns the path to a fresh ZIP inside
the caller-provided (empty) directory.
"""

from __future__ import annotations

import io
import os
import zipfile

META = (
    '<archive xmlns="http://rs.tdwg.org/dwc/text/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:schemaLocation="http://rs.tdwg.org/dwc/text/">'
    "{core}"
    "{extensions}"
    "</archive>"
)

OCC = "http://rs.tdwg.org/dwc/terms/Occurrence"
MEAS = "http://rs.tdwg.org/dwc/terms/MeasurementOrFact"
MEDIA = "http://rs.tdwg.org/dwc/terms/Multimedia"
OCC_TERM = "http://rs.tdwg.org/dwc/terms/occurrenceID"
CAT_TERM = "http://rs.tdwg.org/dwc/terms/catalogNumber"
MEAS_TERM = "http://rs.tdwg.org/dwc/terms/measurementType"
MEAS_VAL = "http://rs.tdwg.org/dwc/terms/measurementValue"
MEDIA_TERM = "http://rs.tdwg.org/dwc/terms/identifier"
MEDIA_FMT = "http://purl.org/dc/terms/format"
# Same local name "note" under two different namespaces: must stay separate.
NOTE_A = "http://rs.tdwg.org/dwc/terms/note"
NOTE_B = "http://example.org/ns-alpha/note"
NOTE_C = "http://example.org/ns-beta/note"
UNMAPPED_TERM = "http://example.org/unmapped/ignoredColumn"
DEFAULT_TERM = "http://rs.tdwg.org/dwc/terms/establishmentMeans"


def field_xml(term: str, index=None, default=None) -> str:
    attrs = f'term="{_xml_attr(term)}"'
    if index is not None:
        attrs += f' index="{index}"'
    if default is not None:
        attrs += f' default="{_xml_attr(default)}"'
    return f"<field {attrs}/>"


def _xml_attr(value) -> str:
    """Escape a value for use inside an XML double-quoted attribute."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
    )


def core_xml(member: str, fields: list[str], id_index: int,
             encoding="UTF-8", delim=",", line="\\n", quote='"',
             header=1, extra_attrib="") -> str:
    return (
        f'<core rowType="{_xml_attr(OCC)}" encoding="{_xml_attr(encoding)}" '
        f'fieldsTerminatedBy="{_xml_attr(delim)}" '
        f'linesTerminatedBy="{_xml_attr(line)}" '
        f'fieldsEnclosedBy="{_xml_attr(quote)}" '
        f'ignoreHeaderLines="{header}" {extra_attrib}>'
        f"<files><location>{member}</location></files>"
        f'<id index="{id_index}"/>'
        + "".join(fields)
        + "</core>"
    )


def ext_xml(row_type: str, member: str, fields: list[str], coreid_index: int,
            encoding="UTF-8", delim=",", line="\\n", quote='"',
            header=1) -> str:
    return (
        f'<extension rowType="{_xml_attr(row_type)}" '
        f'encoding="{_xml_attr(encoding)}" '
        f'fieldsTerminatedBy="{_xml_attr(delim)}" '
        f'linesTerminatedBy="{_xml_attr(line)}" '
        f'fieldsEnclosedBy="{_xml_attr(quote)}" '
        f'ignoreHeaderLines="{header}">'
        f"<files><location>{member}</location></files>"
        f'<coreid index="{coreid_index}"/>'
        + "".join(fields)
        + "</extension>"
    )


def write_zip(path: str, members: list[tuple[str, bytes]],
              meta_xml: str | None = None) -> str:
    """Write members in the exact order given (ZIP order matters for tests)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
        if meta_xml is not None:
            zf.writestr("meta.xml", meta_xml.encode("utf-8"))
    return path


def csv_bytes(rows: list[list[str]], delim: str = ",",
              line: str = "\n", quote: str = '"',
              encoding: str = "utf-8") -> bytes:
    def enc(cell: str) -> str:
        if quote and (delim in cell or "\n" in cell or quote in cell):
            return quote + cell.replace(quote, quote + quote) + quote
        return cell

    text = "".join(
        delim.join(enc(c) for c in row) + line for row in rows
    )
    return text.encode(encoding)


# ---------------------------------------------------------------------------
# Main feature archive
# ---------------------------------------------------------------------------

def build_feature_archive(path: str) -> dict:
    """The headline scenario with explicit, independently written expected
    facts (returned as the dict)."""
    core_rows = [
        ["id", "occurrenceID", "catalogNumber", "noteA", "noteB",
         "establishmentMeans"],
        # C1: two measurements, three media; empty cell gets the declared
        # default; noteA vs noteB share local name "note".
        ["C1", "OCC-SHARED", "cat-1", "alpha note", "", ""],
        # C2: shares occurrenceID with C1 -- still two distinct core records.
        ["C2", "OCC-SHARED", "cat-2", "beta", "beta-b", "wild"],
        # C3: fine core that an extension will fail to reference.
        ["C3", "OCC-3", "cat-3", "gamma", "gamma-b", "wild"],
        # C4: short row (mapped column out of range) but non-empty id; an
        # extension references it (unique-but-bad core reference).
        ["C4", "OCC-4"],
        # C5 then C5 again: duplicate key only visible at the last row.
        ["C5", "OCC-5", "cat-5", "epsilon", "eps-b", "captive"],
        ["C5", "OCC-5-DUP", "cat-5b", "epsilon2", "eps2-b", "captive"],
        # C6: empty id -- invalid, never referenceable.
        ["", "OCC-6", "cat-6", "zeta", "zeta-b", "wild"],
    ]
    core_data = csv_bytes(core_rows)

    meas_rows = [
        ["coreid", "measurementType", "measurementValue", "noteC"],
        ["C1", "weight", "12", "first"],
        ["C1", "length", "34", "second"],
        ["C3", "weight", "9", ""],            # default-only term tested below
        ["C4", "weight", "7", "on-bad-core"],  # unique bad core reference
        ["C5", "weight", "5", "on-ambiguous"],  # ambiguous reference
        ["GHOST", "weight", "1", "orphan"],     # missing reference
        ["", "weight", "2", "empty-ref"],       # empty reference
        ["C1", "weight", "12", "first"],        # exact duplicate content: keep
    ]
    meas_data = csv_bytes(meas_rows)

    media_rows = [
        ["coreid", "identifier", "format"],
        ["C1", "m1.jpg", "image/jpeg"],
        ["C1", "m2.jpg", "image/jpeg"],
        ["C1", "m3.jpg", "image/jpeg"],
        ["C2", "m4.jpg", "image/jpeg"],
    ]
    media_data = csv_bytes(media_rows)

    core_fields = [
        field_xml(OCC_TERM, 1),
        field_xml(CAT_TERM, 2),
        field_xml(NOTE_A, 3),
        field_xml(NOTE_B, 4),
        field_xml(DEFAULT_TERM, 5, default="wild"),
    ]
    meas_fields = [
        field_xml(MEAS_TERM, 1),
        field_xml(MEAS_VAL, 2),
        field_xml(NOTE_C, 3),
        field_xml(DEFAULT_TERM, default="assumed"),  # no index -> default only
    ]
    media_fields = [
        field_xml(MEDIA_TERM, 1),
        field_xml(MEDIA_FMT, 2),
    ]
    meta = META.format(
        core=core_xml("data/core.csv", core_fields, 0),
        extensions=(
            ext_xml(MEAS, "data/measurements.csv", meas_fields, 0)
            + ext_xml(MEDIA, "data/media.csv", media_fields, 0)
        ),
    )

    # Extension members deliberately precede the core member in ZIP order.
    members = [
        ("data/measurements.csv", meas_data),
        ("data/media.csv", media_data),
        ("data/core.csv", core_data),
    ]
    write_zip(path, members, meta)

    return {
        "core_input": len(core_rows) - 1,
        "meas_input": len(meas_rows) - 1,
        "media_input": len(media_rows) - 1,
        "valid_core_ids": ["C1", "C2", "C3"],
        "shared_occurrence_id": "OCC-SHARED",
        "c1_measurement_types": ["weight", "length", "weight"],
        "c1_media_count": 3,
    }


# ---------------------------------------------------------------------------
# Dialect archive: reordered columns, unmapped column, BOM, Latin-1, tabs,
# quoted newline, short rows, defaulted empty cells.
# ---------------------------------------------------------------------------

def build_dialect_archive(path: str) -> None:
    # Tab-separated, CRLF, quoted embedded newline; column order deliberately
    # differs from the feature archive; column 3 is unmapped.
    core_header = ["id", "catalogNumber", "unmapped", "occurrenceID", "noteA"]
    core_rows_data = [
        core_header,
        ["D1", "cat-d1", "skip-me", "OCC-D1", "line1\nline2"],
        ["D2", "cat-d2", "skip-me", "OCC-D2", "plain"],
        ["D3"],  # short row: every mapped index beyond 0 is out of range
    ]
    core_data = csv_bytes(
        core_rows_data, delim="\t", line="\r\n"
    ).decode("utf-8").encode("utf-8-sig")  # UTF-8 BOM

    # Latin-1 extension with a byte that is invalid UTF-8 (0xE9 = é).
    ext_rows_data = [
        ["coreid", "measurementType", "measurementValue"],
        ["D1", "café", "1"],
        ["D2", "", "2"],  # empty cell -> default below
    ]
    ext_data = csv_bytes(ext_rows_data, delim="\t", line="\r\n",
                         encoding="latin-1")

    core_fields = [
        field_xml(CAT_TERM, 1),
        field_xml(OCC_TERM, 3),
        field_xml(NOTE_A, 4),
    ]
    ext_fields = [
        field_xml(MEAS_TERM, 1, default="unknown-measurement"),
        field_xml(MEAS_VAL, 2),
    ]
    meta = META.format(
        core=core_xml("core.tsv", core_fields, 0,
                      encoding="UTF-8", delim="\\t", line="\\r\\n"),
        extensions=ext_xml(MEAS, "meas.tsv", ext_fields, 0,
                           encoding="ISO-8859-1", delim="\\t",
                           line="\\r\\n"),
    )
    write_zip(path, [("core.tsv", core_data), ("meas.tsv", ext_data)], meta)


# ---------------------------------------------------------------------------
# Descriptor failures
# ---------------------------------------------------------------------------

def build_bad_descriptor_zip(path: str, meta_xml: str,
                             members: list[tuple[str, bytes]] | None = None
                             ) -> None:
    write_zip(path, members or [("data/core.csv", b"id\nC1\n")], meta_xml)


def minimal_core_meta(**overrides) -> str:
    kwargs = dict(
        member="data/core.csv",
        fields=[field_xml(OCC_TERM, 1)],
        id_index=0,
    )
    kwargs.update(overrides)
    return META.format(core=core_xml(**kwargs), extensions="")


def build_traversal_zip(path: str) -> None:
    meta = minimal_core_meta(member="../escape.csv")
    write_zip(path, [("../escape.csv", b"id,x\nC1,y\n")], meta)
