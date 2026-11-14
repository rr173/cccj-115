"""Catalogue, type registry and limit validation."""

from __future__ import annotations

import json

import pytest

from pgcopy_bridge import Catalogue, DescriptorError, Limits, load_catalogue
from pgcopy_bridge.catalogue import (
    catalogue_from_mapping,
    normalise_type,
    supported_type_names,
)


def test_supported_type_set_is_exactly_the_contract():
    assert set(supported_type_names()) == {
        "int4", "int8", "bool", "text", "bytea", "uuid", "numeric", "date",
        "timestamp", "timestamptz", "int4[]", "text[]", "numeric[]",
    }


@pytest.mark.parametrize("declared, canonical", [
    ("int4", "int4"),
    ("integer", "int4"),
    ("INT8", "int8"),
    ("bigint", "int8"),
    ("timestamp with time zone", "timestamptz"),
    ("timestamp without time zone", "timestamp"),
    ("numeric[]", "numeric[]"),
    ("integer[]", "int4[]"),
    (" text [] ", "text[]"),
])
def test_type_aliases(declared, canonical):
    assert normalise_type(declared) == canonical


@pytest.mark.parametrize("declared", ["float8", "money", "jsonb", "bytea[]",
                                      "uuid[]", "date[]", "int2", ""])
def test_unsupported_types_are_rejected(declared):
    with pytest.raises(DescriptorError):
        normalise_type(declared)


def test_catalogue_keeps_order_and_requires_unique_names():
    doc = {"columns": [{"name": "b", "type": "int4"},
                       {"name": "a", "type": "text"}]}
    cat = catalogue_from_mapping(doc)
    assert cat.names == ("b", "a")


def test_duplicate_names_rejected_case_sensitively():
    doc = {"columns": [{"name": "x", "type": "int4"},
                       {"name": "x", "type": "text"}]}
    with pytest.raises(DescriptorError, match="duplicate column name"):
        catalogue_from_mapping(doc)


def test_empty_catalogue_rejected():
    with pytest.raises(DescriptorError):
        catalogue_from_mapping({"columns": []})


def test_missing_type_rejected():
    with pytest.raises(DescriptorError, match="type"):
        catalogue_from_mapping({"columns": [{"name": "a"}]})


def test_unknown_keys_rejected():
    with pytest.raises(DescriptorError, match="unknown keys"):
        catalogue_from_mapping(
            {"columns": [{"name": "a", "type": "int4", "nullable": True}]})


def test_catalogue_file_roundtrip(tmp_path, catalogue_path):
    cat = load_catalogue(catalogue_path)
    assert len(cat) == 13
    assert cat.columns[0].type.name == "int4"


def test_catalogue_file_is_utf8_with_bom(tmp_path):
    path = tmp_path / "cat.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"columns": [{"name": "é", "type": "text"}]}).encode())
    assert load_catalogue(str(path)).names == ("é",)


def test_limits_defaults():
    limits = Limits()
    assert limits.max_field_bytes == 64 * 1024 * 1024
    assert limits.max_array_dims == 6
    assert limits.max_array_elements == 4_000_000


@pytest.mark.parametrize("kwargs", [
    {"max_field_bytes": -1},
    {"max_array_dims": -1},
    {"max_array_elements": -1},
])
def test_limits_reject_negative(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_limits_describe_order():
    assert [k for k, _ in Limits().describe()] == [
        "max_field_bytes", "max_array_dims", "max_array_elements"]
