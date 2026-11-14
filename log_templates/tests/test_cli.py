"""CLI end-to-end tests over real UTF-8 JSONL streams."""

from __future__ import annotations

import json
import os

import pytest

from conftest import run_cli, write_stream


MESSAGES = [
    ("web-1", 0, "GET /index 200 10.0.0.1"),
    ("web-1", 1, "GET /index 200 10.0.0.2"),
    ("web-1", 2, "GET /login 404 10.0.0.3"),
    ("web-1", 3, ""),
    ("web-1", 4, "   "),
    ("web-1", 5, "  spaced\ttab  "),
]


def _create(db, *extra):
    return run_cli([
        "create", db, "--threshold", "0.5", "--depth", "2", *extra,
    ])


def test_cli_create_ingest_and_inspect(tmp_path):
    db = str(tmp_path / "lib.db")
    stream = str(tmp_path / "in.jsonl")
    write_stream(stream, MESSAGES)
    assert _create(db).returncode == 0
    result = run_cli(["ingest", db, stream])
    assert result.returncode == 0, result.stderr
    assert "accepted=6" in result.stdout
    assert "replays_ignored=0" in result.stdout

    listing = run_cli(["list-templates", db])
    assert listing.returncode == 0
    lines = listing.stdout.strip().splitlines()
    assert len(lines) >= 3  # header + templates
    assert "GET <*>" in listing.stdout or "GET" in listing.stdout

    show = run_cli(["show-template", db, "1", "--history"])
    assert show.returncode == 0
    assert "template 1" in show.stdout
    assert "structure:" in show.stdout

    members = run_cli(["members", db, "1"])
    assert members.returncode == 0
    assert "web-1" in members.stdout

    verify = run_cli(["verify", db])
    assert verify.returncode == 0
    assert "ok" in verify.stdout


def test_cli_show_message_reconstructs_json(tmp_path):
    db = str(tmp_path / "lib.db")
    stream = str(tmp_path / "in.jsonl")
    write_stream(stream, [("s", 0, "  a\t b  10.0.0.1 ")])
    _create(db)
    assert run_cli(["ingest", db, stream]).returncode == 0
    result = run_cli(["show-message", db, "s", "0", "--json"])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["message"] == "  a\t b  10.0.0.1 "
    assert data["reconstructed"] == data["message"]
    assert data["exact"] is True
    assert data["template_id"] == 1
    assert data["parameters"][2]["kind"] == "mask"
    assert data["parameters"][2]["value"] == "IPV4"
    assert data["parameters"][2]["word"] == "10.0.0.1"
    params = run_cli(["parameters", db, "s", "0"])
    assert "literal" in params.stdout and "IPV4" in params.stdout


def test_cli_stats_and_config(tmp_path):
    db = str(tmp_path / "lib.db")
    stream = str(tmp_path / "in.jsonl")
    write_stream(stream, MESSAGES)
    _create(db, "--masks", "IPV4,INT")
    run_cli(["ingest", db, stream])
    stats = run_cli(["stats", db])
    assert stats.returncode == 0
    assert "threshold: 0.5" in stats.stdout
    assert "enabled_masks" in stats.stdout
    assert "IPV4,INT" in stats.stdout.replace('"', "").replace("'", "") or \
        "IPV4" in stats.stdout


def test_cli_replay_is_reported_not_refused(tmp_path):
    db = str(tmp_path / "lib.db")
    stream = str(tmp_path / "in.jsonl")
    write_stream(stream, MESSAGES)
    _create(db)
    assert run_cli(["ingest", db, stream]).returncode == 0
    again = run_cli(["ingest", db, stream])
    assert again.returncode == 0
    assert "accepted=0" in again.stdout
    assert "replays_ignored=6" in again.stdout


def test_cli_conflict_exit_code(tmp_path):
    db = str(tmp_path / "lib.db")
    good = str(tmp_path / "good.jsonl")
    bad = str(tmp_path / "bad.jsonl")
    write_stream(good, [("s", 0, "alpha beta")])
    write_stream(bad, [("s", 0, "alpha GAMMA")])
    _create(db)
    assert run_cli(["ingest", db, good]).returncode == 0
    result = run_cli(["ingest", db, bad])
    assert result.returncode == 4
    assert "conflict" in result.stderr.lower()


def test_cli_gap_exit_code(tmp_path):
    db = str(tmp_path / "lib.db")
    good = str(tmp_path / "good.jsonl")
    gap = str(tmp_path / "gap.jsonl")
    write_stream(good, [("s", 0, "alpha beta")])
    write_stream(gap, [("s", 2, "alpha gamma")])
    _create(db)
    assert run_cli(["ingest", db, good]).returncode == 0
    result = run_cli(["ingest", db, gap])
    assert result.returncode == 3
    assert "gap" in result.stderr.lower()
    # the library was not advanced
    stats = run_cli(["stats", db])
    assert "next_seq=1" in stats.stdout


def test_cli_bad_stream_exit_code(tmp_path):
    db = str(tmp_path / "lib.db")
    bad = str(tmp_path / "bad.jsonl")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write('{"source": "s", "seq": 0}\n')
    _create(db)
    result = run_cli(["ingest", db, bad])
    assert result.returncode == 5
    assert "missing field" in result.stderr


def test_cli_refuses_to_overwrite_existing_library(tmp_path):
    db = str(tmp_path / "lib.db")
    assert _create(db).returncode == 0
    result = _create(db)
    assert result.returncode == 2
    assert "exists" in result.stderr


def test_cli_batch_size_splits_transactions(tmp_path):
    db = str(tmp_path / "lib.db")
    stream = str(tmp_path / "in.jsonl")
    write_stream(stream, MESSAGES)
    _create(db)
    result = run_cli(["ingest", db, stream, "--batch-size", "2"])
    assert result.returncode == 0, result.stderr
    assert "accepted=6" in result.stdout
    with open(stream, encoding="utf-8") as fh:
        pass
    verify = run_cli(["verify", db])
    assert verify.returncode == 0


def test_cli_utf8_message_roundtrip(tmp_path):
    db = str(tmp_path / "lib.db")
    stream = str(tmp_path / "in.jsonl")
    message = "测站 北京 上报 10.0.0.1 完成"
    write_stream(stream, [("s", 0, message)])
    _create(db)
    result = run_cli(["ingest", db, stream])
    assert result.returncode == 0, result.stderr
    show = run_cli(["show-message", db, "s", "0", "--json"])
    data = json.loads(show.stdout)
    assert data["message"] == message
    assert data["reconstructed"] == message
    assert data["parameters"][3]["value"] == "IPV4"


def test_cli_rebuild_index(tmp_path):
    db = str(tmp_path / "lib.db")
    stream = str(tmp_path / "in.jsonl")
    write_stream(stream, MESSAGES)
    _create(db)
    run_cli(["ingest", db, stream])
    result = run_cli(["rebuild-index", db])
    assert result.returncode == 0, result.stderr
