"""Parsing and strict validation of the DwC-A ``meta.xml`` descriptor.

Only the documented subset is accepted; anything else raises
:class:`~dwca.errors.DescriptorError` *before* any data file is read.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Optional

from .errors import DescriptorError
from .models import ArchiveSpec, FieldSpec, TableSpec

DWC_TEXT_NS = "http://rs.tdwg.org/dwc/text/"
OCCURRENCE_ROW_TYPE = "http://rs.tdwg.org/dwc/terms/Occurrence"

# Dialect whitelist.  Everything else is an explicit, unsupported dialect.
_ENCODINGS = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "utf8sig": "utf-8-sig",
    "latin-1": "latin-1",
    "latin1": "latin-1",
    "iso-8859-1": "latin-1",
    "iso8859-1": "latin-1",
    "iso8859_1": "latin-1",
}
_FIELD_DELIMITERS = {",": ",", "\t": "\t"}
_LINE_TERMINATORS = {"\n": "\n", "\r\n": "\r\n"}
_QUOTE_CHARS = {'"': '"', "": ""}
_INT_RE = re.compile(r"^-?\d+$")


def _local(tag: str) -> str:
    """ElementTree tag without XML namespace."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _children(element, name: str):
    return [c for c in element if _local(c.tag) == name]


def _one(element, name: str, required: bool = True):
    found = _children(element, name)
    if len(found) > 1:
        raise DescriptorError(
            f"descriptor: at most one <{name}> expected under "
            f"<{_local(element.tag)}>, found {len(found)}"
        )
    if not found:
        if required:
            raise DescriptorError(
                f"descriptor: required <{name}> missing under "
                f"<{_local(element.tag)}>"
            )
        return None
    return found[0]


def decode_escaped(raw: str, attr: str) -> str:
    """Decode the backslash escapes DwC-A descriptors use for delimiters.

    Supports ``\\t`` (tab), ``\\n`` (LF), ``\\r`` (CR, incl. ``\\r\\n``) and
    ``\\\\`` (literal backslash).  A dangling backslash or any other escape
    is rejected.  Literal control characters are also passed through, so a
    descriptor carrying a real tab works too.
    """
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(raw):
            raise DescriptorError(
                f"descriptor: dangling backslash escape in {attr}={raw!r}"
            )
        nxt = raw[i + 1]
        mapping = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
        if nxt not in mapping:
            raise DescriptorError(
                f"descriptor: unsupported backslash escape '\\{nxt}' in "
                f"{attr}={raw!r}"
            )
        out.append(mapping[nxt])
        i += 2
    return "".join(out)


def _attr(element, name: str) -> Optional[str]:
    # ElementTree exposes namespace-less attribute lookups directly.
    return element.attrib.get(name)


def _require_attr(element, name: str) -> str:
    value = _attr(element, name)
    if value is None or value == "":
        raise DescriptorError(
            f"descriptor: <{_local(element.tag)}> requires non-empty "
            f"attribute {name!r}"
        )
    return value


def _parse_index(element) -> Optional[int]:
    raw = _attr(element, "index")
    if raw is None:
        return None
    if not _INT_RE.match(raw):
        raise DescriptorError(
            f"descriptor: index={raw!r} is not an integer on "
            f"<{_local(element.tag)} term={_attr(element, 'term')!r}>"
        )
    idx = int(raw)
    if idx < 0:
        raise DescriptorError(
            f"descriptor: negative column index {idx} on "
            f"<{_local(element.tag)} term={_attr(element, 'term')!r}>"
        )
    return idx


def _parse_field(element) -> FieldSpec:
    term = _require_attr(element, "term")
    index = _parse_index(element)
    default = _attr(element, "default")
    if index is None and default is None:
        raise DescriptorError(
            f"descriptor: field {term!r} has neither index nor default"
        )
    return FieldSpec(term=term, index=index, default=default)


def _safe_member_name(location: str, available: frozenset[str]) -> str:
    """Validate a files/location against the ZIP's own member list.

    Members are read only from this ZIP: absolute paths, drive letters,
    backslash paths and ``..`` traversal are all rejected, and the normalized
    name must match an entry physically present.
    """
    if location == "":
        raise DescriptorError("descriptor: empty files/location")
    if location.endswith("/"):
        raise DescriptorError(
            f"descriptor: member {location!r} is a directory entry, not a "
            "data file"
        )
    if "\\" in location:
        raise DescriptorError(
            f"descriptor: member {location!r} uses backslashes; ZIP members "
            "use forward slashes"
        )
    if posixpath.isabs(location) or re.match(r"^[A-Za-z]:", location):
        raise DescriptorError(
            f"descriptor: member {location!r} is an absolute/drive path"
        )
    normalized = posixpath.normpath(location)
    if normalized.startswith("../") or normalized == ".." or "/../" in (
        "/" + normalized + "/"
    ):
        raise DescriptorError(
            f"descriptor: member {location!r} escapes the archive root"
        )
    if normalized not in available:
        raise DescriptorError(
            f"descriptor: declared member {location!r} is missing from the "
            "ZIP (normalized name checked)"
        )
    return normalized


def _parse_table(element, available: frozenset[str], is_core: bool) -> TableSpec:
    kind = "core" if is_core else "extension"
    row_type = _require_attr(element, "rowType")
    if is_core and row_type != OCCURRENCE_ROW_TYPE:
        raise DescriptorError(
            f"descriptor: only an Occurrence core is supported, got "
            f"rowType={row_type!r}"
        )

    encoding_raw = _attr(element, "encoding") or "UTF-8"
    encoding = _ENCODINGS.get(encoding_raw.strip().lower())
    if encoding is None:
        raise DescriptorError(
            f"descriptor: unsupported encoding {encoding_raw!r}; supported "
            "subset is UTF-8, UTF-8 with BOM and Latin-1 (ISO-8859-1)"
        )

    delim_raw = _attr(element, "fieldsTerminatedBy") or ","
    delim = decode_escaped(delim_raw, "fieldsTerminatedBy")
    if delim not in _FIELD_DELIMITERS:
        raise DescriptorError(
            f"descriptor: unsupported fieldsTerminatedBy={delim_raw!r}; "
            "only comma and tab are supported"
        )

    line_raw = _attr(element, "linesTerminatedBy") or "\\n"
    line_term = decode_escaped(line_raw, "linesTerminatedBy")
    if line_term not in _LINE_TERMINATORS:
        raise DescriptorError(
            f"descriptor: unsupported linesTerminatedBy={line_raw!r}; only "
            "LF and CRLF are supported"
        )

    quote_raw = _attr(element, "fieldsEnclosedBy")
    if quote_raw is None:
        quote = '"'  # DwC default
    else:
        quote = decode_escaped(quote_raw, "fieldsEnclosedBy")
    if quote not in _QUOTE_CHARS:
        raise DescriptorError(
            f"descriptor: unsupported fieldsEnclosedBy={quote_raw!r}; only "
            'the double quote (or empty for no quoting) is supported'
        )

    header_raw = _attr(element, "ignoreHeaderLines") or "0"
    if not _INT_RE.match(header_raw):
        raise DescriptorError(
            f"descriptor: ignoreHeaderLines={header_raw!r} is not an integer"
        )
    ignore_header_lines = int(header_raw)
    if ignore_header_lines < 0:
        raise DescriptorError(
            f"descriptor: ignoreHeaderLines must be non-negative, got "
            f"{ignore_header_lines}"
        )

    files_el = _one(element, "files")
    locations = _children(files_el, "location")
    if len(locations) != 1:
        raise DescriptorError(
            f"descriptor: each table must declare exactly one files/location, "
            f"{kind} with rowType={row_type!r} declares {len(locations)}"
        )
    member = _safe_member_name((locations[0].text or "").strip(), available)

    id_field: Optional[FieldSpec] = None
    coreid_field: Optional[FieldSpec] = None
    if is_core:
        id_el = _one(element, "id")
        id_index = _parse_index(id_el)
        if id_index is None:
            raise DescriptorError("descriptor: core <id> requires an index")
        id_field = FieldSpec(term="", index=id_index, default=None, is_id=True)
    else:
        coreid_el = _one(element, "coreid")
        coreid_index = _parse_index(coreid_el)
        if coreid_index is None:
            raise DescriptorError(
                "descriptor: extension <coreid> requires an index"
            )
        coreid_field = FieldSpec(
            term="", index=coreid_index, default=None, is_id=True
        )

    fields: list[FieldSpec] = []
    seen_terms: set[str] = set()
    for field_el in _children(element, "field"):
        spec = _parse_field(field_el)
        if spec.term in seen_terms:
            raise DescriptorError(
                f"descriptor: duplicate field term {spec.term!r} in {kind} "
                f"table {row_type!r}"
            )
        seen_terms.add(spec.term)
        fields.append(spec)

    table_id = "core" if is_core else f"ext:{row_type}"
    return TableSpec(
        table_id=table_id,
        row_type=row_type,
        member=member,
        encoding=encoding,
        fields_terminated_by=delim,
        lines_terminated_by=line_term,
        fields_enclosed_by=quote,
        ignore_header_lines=ignore_header_lines,
        id_field=id_field,
        coreid_field=coreid_field,
        fields=tuple(fields),
    )


def parse_archive(zip_file: zipfile.ZipFile, xml_bytes: bytes) -> ArchiveSpec:
    """Validate ``meta.xml`` plus member layout and return an ArchiveSpec."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise DescriptorError(f"descriptor: meta.xml is not valid XML: {exc}")

    if _local(root.tag) != "archive":
        raise DescriptorError(
            f"descriptor: root element must be <archive>, got "
            f"<{_local(root.tag)}>"
        )

    cores = _children(root, "core")
    if len(cores) != 1:
        raise DescriptorError(
            f"descriptor: exactly one Occurrence <core> is required, found "
            f"{len(cores)}"
        )

    available = frozenset(zip_file.namelist())
    core_spec = _parse_table(cores[0], available, is_core=True)

    extensions: list[TableSpec] = []
    seen_ext_types: set[str] = set()
    used_members = {core_spec.member}
    for ext_el in _children(root, "extension"):
        ext_spec = _parse_table(ext_el, available, is_core=False)
        if ext_spec.row_type in seen_ext_types:
            raise DescriptorError(
                f"descriptor: duplicate extension rowType "
                f"{ext_spec.row_type!r}; each extension needs a distinct "
                "row type in this subset"
            )
        if ext_spec.member in used_members:
            raise DescriptorError(
                f"descriptor: member {ext_spec.member!r} is mapped by more "
                "than one table"
            )
        seen_ext_types.add(ext_spec.row_type)
        used_members.add(ext_spec.member)
        extensions.append(ext_spec)

    return ArchiveSpec(
        core=core_spec,
        extensions=tuple(extensions),
        members=available,
        xml_bytes=xml_bytes,
    )
