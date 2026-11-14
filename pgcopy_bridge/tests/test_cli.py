"""CLI behaviour, process exit codes and the stdio publication rule."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from pgcopy_bridge import load_catalogue
from pgcopy_bridge.cli import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args, stdin_bytes=None, cwd=ROOT):
    env = {**os.environ, "PYTHONPATH": ROOT}
    return subprocess.run(
        [sys.executable, "-s", "-m", "pgcopy_bridge.cli", *args],
        cwd=cwd, env=env, input=stdin_bytes, capture_output=True,
        check=False)


def test_decode_and_encode_via_cli(tmp_path, materials_dir):
    out = tmp_path / "out.jsonl"
    r = run_cli("decode", os.path.join(materials_dir, "catalogue.json"),
                os.path.join(materials_dir, "sample.copy"), str(out),
                "--read-chunk", "1")
    assert r.returncode == 0, r.stderr
    assert b"decoded 2 tuple(s)" in r.stdout
    back = tmp_path / "back.copy"
    r = run_cli("encode", os.path.join(materials_dir, "catalogue.json"),
                str(out), str(back), "--read-chunk", "1")
    assert r.returncode == 0, r.stderr
    assert back.read_bytes() == open(
        os.path.join(materials_dir, "sample.copy"), "rb").read()


def test_inspect_reports_counts_and_limits(tmp_path, materials_dir):
    r = run_cli("inspect", os.path.join(materials_dir, "catalogue.json"),
                os.path.join(materials_dir, "sample.copy"),
                "--max-field-bytes", "1000")
    assert r.returncode == 0, r.stderr
    text = r.stdout.decode()
    assert "columns: 13" in text
    assert "tuples: 2" in text
    assert "max_field_bytes=1000" in text
    assert "max_array_elements=4000000" in text
    # Per-column NULL counts are reported.
    row = [line for line in text.splitlines() if line.startswith(" 1  ")][0]
    assert row.split()[3] == "1"


def test_inspect_prints_no_business_values(tmp_path, materials_dir):
    r = run_cli("inspect", os.path.join(materials_dir, "catalogue.json"),
                os.path.join(materials_dir, "sample.copy"))
    text = r.stdout.decode()
    assert "123e4567" not in text
    assert "1234567890" not in text
    assert "00ff1080" not in text
    assert "-9007199254740993" not in text


def test_types_subcommand_documents_every_type():
    r = run_cli("types")
    assert r.returncode == 0
    text = r.stdout.decode()
    for type_name in ("int4", "int8", "bool", "text", "bytea", "uuid",
                      "numeric", "date", "timestamp", "timestamptz",
                      "int4[]", "text[]", "numeric[]"):
        assert type_name in text
    assert "0.0000" in text
    assert "-2147483648" in text
    assert "\"shape\"" in text


def test_cli_error_has_location_and_exit_code(tmp_path, materials_dir):
    bad = tmp_path / "bad.copy"
    data = bytearray(open(os.path.join(materials_dir, "sample.copy"),
                          "rb").read())
    data[0] = 0x00
    bad.write_bytes(bytes(data))
    dest = tmp_path / "dest.jsonl"
    dest.write_bytes(b"untouched")
    r = run_cli("decode", os.path.join(materials_dir, "catalogue.json"),
                str(bad), str(dest))
    assert r.returncode == 1
    assert b"byte 0" in r.stderr
    assert b"signature" in r.stderr
    assert dest.read_bytes() == b"untouched"


def test_stdin_used_when_input_is_dash(tmp_path, materials_dir):
    data = open(os.path.join(materials_dir, "sample.copy"), "rb").read()
    out = tmp_path / "out.jsonl"
    r = run_cli("decode", os.path.join(materials_dir, "catalogue.json"),
                "-", str(out), stdin_bytes=data, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == open(
        os.path.join(materials_dir, "sample.jsonl"), "rb").read()


def test_stdin_bad_input_leaves_target(tmp_path, materials_dir):
    data = open(os.path.join(materials_dir, "sample.copy"), "rb").read()
    out = tmp_path / "out.jsonl"
    out.write_bytes(b"PRIOR")
    r = run_cli("decode", os.path.join(materials_dir, "catalogue.json"),
                "-", str(out), stdin_bytes=data[:-2], cwd=str(tmp_path))
    assert r.returncode == 1
    assert out.read_bytes() == b"PRIOR"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_inspect_bad_file_exits_1(tmp_path, materials_dir):
    bad = tmp_path / "bad.copy"
    bad.write_bytes(b"NOTPGCOPY")
    r = run_cli("inspect", os.path.join(materials_dir, "catalogue.json"),
                str(bad))
    assert r.returncode == 1
    assert b"signature" in r.stderr


def test_catalogue_template_is_valid(tmp_path):
    r = run_cli("catalogue-template")
    assert r.returncode == 0
    doc = json.loads(r.stdout.decode())
    assert len(doc["columns"]) == 12
    path = tmp_path / "cat.json"
    path.write_bytes(r.stdout)
    load_catalogue(str(path))


def test_cli_documents_stdin_publication_rule():
    import pgcopy_bridge.cli as cli
    assert "standard input" in cli.__doc__.lower()
    assert "atomically" in cli.__doc__.lower()
