"""Hand-written materials vs. an independent byte walk and expected JSON."""

from __future__ import annotations

import json
import os

import pytest

import _common as B
from independent_checker import BadBytes, inspect
from pgcopy_bridge import load_catalogue
from pgcopy_bridge.library import decode_file, encode_file
from pgcopy_bridge.numeric import to_text


def _load_jsonl(path):
    with open(path, "rb") as fh:
        return [json.loads(line) for line in fh]


def test_expected_files_exist(materials_dir):
    for name in ("sample.copy", "sample.jsonl", "arrays.copy",
                 "arrays.jsonl", "empty.copy", "catalogue.json",
                 "arrays_catalogue.json", "interop.sql", "manifest.json",
                 "sample.offsets.txt", "arrays.offsets.txt"):
        assert os.path.exists(os.path.join(materials_dir, name)), name


def test_manifest_hashes_match(materials_dir):
    import hashlib
    with open(os.path.join(materials_dir, "manifest.json"),
              encoding="utf-8") as fh:
        manifest = json.load(fh)
    for name, entry in manifest.items():
        data = open(os.path.join(materials_dir, name), "rb").read()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["bytes"]


def test_sample_bytes_decode_to_hand_written_expectation(materials_dir,
                                                         tmp_path):
    out = tmp_path / "sample.jsonl"
    decode_file(os.path.join(materials_dir, "sample.copy"),
                os.path.join(materials_dir, "catalogue.json"), str(out),
                chunk_size=1)
    assert _load_jsonl(str(out)) == _load_jsonl(
        os.path.join(materials_dir, "sample.jsonl"))


def test_arrays_bytes_decode_to_hand_written_expectation(materials_dir,
                                                         tmp_path):
    out = tmp_path / "arrays.jsonl"
    decode_file(os.path.join(materials_dir, "arrays.copy"),
                os.path.join(materials_dir, "arrays_catalogue.json"),
                str(out), chunk_size=1)
    assert _load_jsonl(str(out)) == _load_jsonl(
        os.path.join(materials_dir, "arrays.jsonl"))


def test_sample_bytes_are_structurally_valid_by_independent_walk(
        materials_dir):
    catalogue = load_catalogue(
        os.path.join(materials_dir, "catalogue.json"))
    data = open(os.path.join(materials_dir, "sample.copy"), "rb").read()
    walked = inspect(data, [c.type.name for c in catalogue.columns])
    assert walked["flags"] == 0
    assert len(walked["tuples"]) == 2
    assert walked["tuples"][0][0] is None
    assert walked["tuples"][0][3]["value"] == bytes.fromhex("00ff1080")
    # numeric groups, straight from the bytes
    amount = walked["tuples"][0][5]["value"]
    assert (amount["ndigits"], amount["weight"], amount["sign"],
            amount["dscale"]) == (11, 7, 0x0000, 13)
    assert amount["digits"][0] == 12
    assert amount["digits"][-1] == 9000
    zero4 = walked["tuples"][0][6]["value"]
    assert (zero4["ndigits"], zero4["dscale"]) == (0, 4)
    assert walked["tuples"][0][7]["value"] == -(2 ** 31)
    assert walked["tuples"][0][8]["value"] == 2 ** 63 - 1
    grid = walked["tuples"][1][10]
    assert grid["dims"] == [(2, 0), (2, -3)]
    assert grid["elements"] == [11, None, 13, 14]


def test_arrays_offsets_file_matches_real_positions(materials_dir):
    data = open(os.path.join(materials_dir, "arrays.copy"), "rb").read()
    with open(os.path.join(materials_dir, "arrays.offsets.txt"),
              encoding="utf-8") as fh:
        text = fh.read()
    assert "29     grid" in text
    assert int.from_bytes(data[29:33], "big", signed=True) == 56
    assert int.from_bytes(data[89:93], "big", signed=True) == 46
    assert int.from_bytes(data[139:143], "big", signed=True) == 48
    assert data[191:193] == b"\x00\x04"
    assert data[352:354] == b"\xff\xff"
    assert len(data) == 354


def test_offsets_file_matches_real_positions(materials_dir):
    data = open(os.path.join(materials_dir, "sample.copy"), "rb").read()
    offsets = {}
    with open(os.path.join(materials_dir, "sample.offsets.txt"),
              encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            position, rest = line.split(None, 1)
            offsets[position] = rest.strip()
    # spot-check the positions called out in the material
    assert data[0:11] == B.SIGNATURE
    assert data[11:15] == b"\x00\x00\x00\x00"
    assert data[15:19] == b"\x00\x00\x00\x00"
    assert data[19:21] == b"\x00\x0d"       # 13 fields
    assert data[21:25] == b"\xff\xff\xff\xff"  # id NULL
    assert int.from_bytes(data[25:29], "big", signed=True) == 8
    assert data[29:37] == B.int8(-9007199254740993)
    assert int.from_bytes(data[37:41], "big", signed=True) == 0
    assert int.from_bytes(data[41:45], "big", signed=True) == 4
    assert data[45:49] == bytes([0x00, 0xFF, 0x10, 0x80])
    assert int.from_bytes(data[69:73], "big", signed=True) == 30
    assert data[73:75] == b"\x00\x0b"       # ndigits 11
    assert data[75:77] == b"\x00\x07"       # weight 7
    assert data[77:79] == b"\x00\x00"       # sign
    assert data[79:81] == b"\x00\x0d"       # dscale 13
    assert int.from_bytes(data[119:123], "big", signed=True) == -(2 ** 31)
    assert data[424:426] == b"\xff\xff"
    assert len(data) == 426
    assert offsets["424"].startswith("end marker")


def test_empty_copy_is_legal_zero_row_file(materials_dir):
    data = open(os.path.join(materials_dir, "empty.copy"), "rb").read()
    assert data == B.build([])
    walked = inspect(data, ["int4", "text"])
    assert walked["tuples"] == []


def test_long_decimal_text_matches_expected(materials_dir):
    records = _load_jsonl(os.path.join(materials_dir, "sample.jsonl"))
    assert to_text(records[0]["amount"]) == \
        "123456789012345678901234567890.1234567890000"
    assert to_text(records[0]["zero4"]) == "0.0000"
    assert records[1]["zero4"]["sign"] == "NaN"
    # date/timestamp sentinel integers survive JSON as integers
    assert isinstance(records[0]["d"], int)
    assert records[0]["ts"] == 2 ** 63 - 1
    assert records[0]["tz"] == -(2 ** 63)


def test_arrays_material_covers_zero_dim_and_negative_bounds(materials_dir):
    records = _load_jsonl(os.path.join(materials_dir, "arrays.jsonl"))
    assert records[0]["grid"]["lower_bounds"] == [0, -3]
    assert records[1]["grid"] == {"shape": [], "lower_bounds": [],
                                  "elements": []}
    assert records[1]["ranks"] is None
    assert records[2]["notes"]["elements"] == ["", None, "q"]
    assert records[2]["grid"]["elements"] == [5, None, -6]
    assert records[2]["ranks"] == {"shape": [0], "lower_bounds": [-2],
                                   "elements": []}


def test_decode_then_encode_reproduces_sample_bytes(materials_dir, tmp_path):
    out_copy = tmp_path / "reencoded.copy"
    encode_file(os.path.join(materials_dir, "sample.jsonl"),
                os.path.join(materials_dir, "catalogue.json"), str(out_copy))
    assert out_copy.read_bytes() == \
        open(os.path.join(materials_dir, "sample.copy"), "rb").read()


def test_independent_checker_validates_reencoded_file(materials_dir,
                                                     tmp_path):
    out_copy = tmp_path / "reencoded.copy"
    encode_file(os.path.join(materials_dir, "sample.jsonl"),
                os.path.join(materials_dir, "catalogue.json"), str(out_copy))
    catalogue = load_catalogue(
        os.path.join(materials_dir, "catalogue.json"))
    walked = inspect(out_copy.read_bytes(),
                     [c.type.name for c in catalogue.columns])
    assert len(walked["tuples"]) == 2
    # every field length implied by the walk consumes exactly the file
    assert walked["tuples"][1][11]["dims"] == [(2, 0), (2, -3)]


def test_independent_checker_rejects_tampering(materials_dir):
    data = bytearray(open(os.path.join(materials_dir, "sample.copy"),
                          "rb").read())
    data[424:426] = b"\x00\x01"
    with pytest.raises(BadBytes):
        inspect(bytes(data), ["int4"] * 13)
