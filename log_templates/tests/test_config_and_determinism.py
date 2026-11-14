"""Config, determinism and depth-as-initialisation-parameter tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, ingest, run_cli
from logtempl.config import ConfigError, build_config, parse_threshold
from logtempl.library import Library, Record


def test_depth_and_caches_are_init_parameters(db_path):
    with Library.create(db_path, threshold="0.5", depth=0,
                        template_cache=4, node_cache=4) as lib:
        ingest(lib, ["a b c"])
        assert lib.template_head(1)["render"] == "a b c"
    # depth may change on reopen: derived index is rebuilt
    with Library.open(db_path, depth=3, template_cache=8,
                      node_cache=8) as lib:
        assert lib.config.depth == 3
        assert lib.config.template_cache == 8
        lib.verify_integrity()
        ingest(lib, ["a b d"], start=1)
    with Library.open(db_path) as lib:
        assert lib.template_head(1)["render"] == "a b <*>"
        lib.verify_integrity()


def test_threshold_is_fixed_for_a_library(db_path):
    with Library.create(db_path, threshold="0.5") as lib:
        assert lib.threshold is not None
    # Opening normally keeps the stored threshold.
    with Library.open(db_path) as lib:
        assert lib.config.threshold_text == "0.5"


def test_zero_depth_means_single_root_bucket(db_path):
    with Library.create(db_path, threshold="1", depth=0) as lib:
        ingest(lib, ["a b c", "d e f", "a b c"])
        # With depth 0 all same-word-count templates share the root node.
        levels = [r[0] for r in lib.conn.execute(
            "SELECT level FROM index_nodes WHERE parent_id IS NULL"
        )]
        assert levels == [0]
        assert len(lib.template_ids()) == 2


def test_word_count_partitions_the_index(db_path):
    with Library.create(db_path, threshold="0.5", depth=2) as lib:
        ingest(lib, ["a b", "a b c", "a b c d"])
        roots = sorted(
            r[0] for r in lib.conn.execute(
                "SELECT slot_value FROM index_nodes WHERE parent_id IS NULL"
            )
        )
        assert roots == ["2", "3", "4"]


def test_python_hash_seed_does_not_change_outcome(tmp_path):
    """Run the same workload under several PYTHONHASHSEED values."""
    messages = [
        "alpha beta gamma delta",
        "alpha x gamma y",
        "zulu beta gamma omega",
        "kilo beta gamma zulu",
        "alpha beta gamma delta",
        "pattern <*> here",
        "pattern x there",
        "job 17 started on node a1",
    ]
    stream = str(tmp_path / "in.jsonl")
    with open(stream, "w", encoding="utf-8") as fh:
        for i, message in enumerate(messages):
            fh.write(json.dumps(
                {"source": "s", "seq": i, "message": message}
            ) + "\n")
    outputs = []
    for seed in ("0", "1", "12345", "random"):
        db = str(tmp_path / f"lib-{seed}.db")
        env = dict(os.environ, PYTHONHASHSEED=seed)
        subprocess.run(
            [sys.executable, "-m", "logtempl.cli", "create", db,
             "--threshold", "0.5", "--depth", "2"],
            check=True, cwd=ROOT, env=env, capture_output=True,
        )
        subprocess.run(
            [sys.executable, "-m", "logtempl.cli", "ingest", db, stream],
            check=True, cwd=ROOT, env=env, capture_output=True,
        )
        listing = subprocess.run(
            [sys.executable, "-m", "logtempl.cli", "list-templates", db],
            check=True, cwd=ROOT, env=env, capture_output=True, text=True,
        )
        shows = []
        for line in listing.stdout.strip().splitlines()[1:]:
            tid = line.split("\t")[0]
            show = subprocess.run(
                [sys.executable, "-m", "logtempl.cli", "show-template", db,
                 tid, "--history"],
                check=True, cwd=ROOT, env=env, capture_output=True, text=True,
            )
            shows.append(show.stdout)
        outputs.append((listing.stdout, shows))
    for other in outputs[1:]:
        assert other == outputs[0]


def test_config_json_is_byte_stable_across_hash_seeds():
    configs = set()
    for seed in ("0", "1", "2", "999999"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run(
            [sys.executable, "-c",
             "from logtempl.config import build_config; "
             "print(build_config(enabled_masks=['INT','UUID','IPV4']).to_json())"],
            cwd=ROOT, env=env, capture_output=True, text=True, check=True,
        )
        configs.add(proc.stdout.strip())
    assert len(configs) == 1
    assert json.loads(configs.pop())["enabled_masks"] == ["IPV4", "UUID", "INT"]


def test_unknown_or_out_of_range_parameters_rejected():
    with pytest.raises(ConfigError):
        build_config(threshold="0")
    with pytest.raises(ConfigError):
        build_config(threshold="1.5")
    with pytest.raises(ConfigError):
        build_config(depth=-1)
    with pytest.raises(ConfigError):
        build_config(template_cache=-1)
    with pytest.raises(ValueError):
        build_config(enabled_masks=["NOPE"])
