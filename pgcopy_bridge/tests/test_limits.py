"""Caller-tunable limits: exact boundary, one-over, and early rejection."""

from __future__ import annotations

import io
import json

import pytest

import _common as B
from pgcopy_bridge import Limits, catalogue_from_mapping, decode_bytes
from pgcopy_bridge.copy_stream import CopyDecoder
from pgcopy_bridge.library import encode_bytes


def cat(*types):
    return catalogue_from_mapping(
        {"columns": [{"name": f"c{i}", "type": t}
                     for i, t in enumerate(types)]})


def decode_with(limits, data, catalogue, chunk_size=1):
    return list(CopyDecoder(io.BytesIO(data), catalogue, limits=limits,
                            chunk_size=chunk_size).iter_tuples())


def test_max_field_bytes_exact_boundary_accepted():
    body = b"x" * 10
    data = B.build([B.tuple_([body])])
    limits = Limits(max_field_bytes=len(body))
    assert decode_with(limits, data, cat("text")) == [["x" * 10]]


def test_max_field_bytes_one_over_rejected():
    body = b"x" * 11
    data = B.build([B.tuple_([body])])
    limits = Limits(max_field_bytes=10)
    with pytest.raises(Exception) as info:
        decode_with(limits, data, cat("text"))
    assert "max_field_bytes" in str(info.value)
    assert info.value.offset == 21  # the length field position


def test_large_declared_length_rejected_before_body_is_read():
    # Declare 2 GiB but only provide the header.  A reader that allocated
    # first would die; ours rejects at the length field.
    raw = (B.header() + (1).to_bytes(2, "big", signed=True)
           + (2 ** 31 - 1).to_bytes(4, "big", signed=True)
           + (-1).to_bytes(2, "big", signed=True))
    limits = Limits(max_field_bytes=1024)
    with pytest.raises(Exception) as info:
        decode_with(limits, raw, cat("text"))
    assert "max_field_bytes" in str(info.value)


def test_max_array_dims_boundary():
    body = B.array([(1, 1)] * 3, 23, [B.int4(1)])
    catalogue = cat("int4[]")
    data = B.build([B.tuple_([body])])
    assert decode_with(Limits(max_array_dims=3), data, catalogue)
    with pytest.raises(Exception) as info:
        decode_with(Limits(max_array_dims=2), data, catalogue)
    assert "max_array_dims" in str(info.value)


def test_max_array_elements_boundary():
    body = B.array([(2, 1), (2, 1)], 23,
                   [B.int4(i) for i in range(4)])
    catalogue = cat("int4[]")
    data = B.build([B.tuple_([body])])
    assert decode_with(Limits(max_array_elements=4), data, catalogue)
    with pytest.raises(Exception) as info:
        decode_with(Limits(max_array_elements=3), data, catalogue)
    assert "max_array_elements" in str(info.value)


def test_array_element_product_checked_before_elements_are_read():
    # shape claims 1000x1000 but the body has no elements; the limit must
    # fire on the declaration, not on an attempt to read a million entries.
    body = B.array([(1000, 1), (1000, 1)], 23, [])
    catalogue = cat("int4[]")
    data = B.build([B.tuple_([body])])
    with pytest.raises(Exception) as info:
        decode_with(Limits(max_array_elements=1000), data, catalogue)
    assert "max_array_elements" in str(info.value)


def test_max_record_bytes_boundary(tmp_path):
    from pgcopy_bridge.library import encode_file
    line = json.dumps({"c0": "x" * 50}) + "\n"
    src = tmp_path / "in.jsonl"
    src.write_bytes(line.encode())
    out = tmp_path / "out.copy"
    cat_path = tmp_path / "cat.json"
    cat_path.write_text(json.dumps({"columns": [{"name": "c0",
                                                 "type": "text"}]}))
    content = len(line) - 1  # bytes before the newline
    encode_file(str(src), str(cat_path), str(out),
                max_record_bytes=content)
    with pytest.raises(Exception) as info:
        encode_file(str(src), str(cat_path), str(out),
                    max_record_bytes=content - 1)
    assert "max_record_bytes" in str(info.value)


def test_limits_on_encoder_for_array_elements():
    catalogue = cat("int4[]")
    value = {"shape": [10], "lower_bounds": [1], "elements": list(range(10))}
    encode_bytes([[value]], catalogue, limits=Limits(max_array_elements=10))
    with pytest.raises(Exception) as info:
        encode_bytes([[value]], catalogue,
                     limits=Limits(max_array_elements=9))
    assert "max_array_elements" in str(info.value)


def test_limits_on_encoder_for_field_bytes():
    catalogue = cat("text")
    encode_bytes([["x" * 10]], catalogue, limits=Limits(max_field_bytes=10))
    with pytest.raises(Exception) as info:
        encode_bytes([["x" * 11]], catalogue,
                     limits=Limits(max_field_bytes=10))
    assert "max_field_bytes" in str(info.value)


def test_zero_limits_reject_any_present_field():
    with pytest.raises(Exception):
        encode_bytes([["x"]], cat("text"), limits=Limits(max_field_bytes=0))
    # NULL has no body, so it passes even with a zero byte limit.
    encode_bytes([[None]], cat("text"), limits=Limits(max_field_bytes=0))


def test_default_limits_permit_material_files(materials_dir):
    from pgcopy_bridge.library import decode_file
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "x.jsonl")
        decode_file(os.path.join(materials_dir, "sample.copy"),
                    os.path.join(materials_dir, "catalogue.json"), out,
                    limits=Limits())
        assert open(out, "rb").read().count(b"\n") == 2
