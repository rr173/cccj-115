"""Expected-materials tests: independent transcript vs. real pipeline.

These tests do not recompute expectations through the production matcher.
They either use the naive reference directly, or execute the CLI and compare
observed facts with the checked-in transcript.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from fractions import Fraction

import pytest

from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "materials"))
import expected_materials as materials  # noqa: E402
import reference as ref  # noqa: E402


def test_transcript_is_independently_reproducible():
    """Regenerating from the reference yields exactly the checked-in file."""
    with open(materials.TRANSCRIPT, encoding="utf-8") as fh:
        stored = json.load(fh)
    assert materials.build_expected() == stored


def test_transcript_scores_are_hand_checkable():
    """Spot-check transcript entries against arithmetic done by hand."""

    with open(materials.TRANSCRIPT, encoding="utf-8") as fh:
        data = json.load(fh)

    tie = data["scenarios"]["tie"]
    # Messages: m0 'aa bb cc dd' -> T1(rev1); m1 'ee ff gg hh' -> T2(rev2);
    # probe m2 'aa bb gg hh'.
    #   vs T1 revision 1 [aa bb cc dd]: aa,bb match = 2/4 = 1/2.
    #   vs T2 revision 2 [ee ff gg hh]: gg,hh match = 2/4 = 1/2.
    # Equal points (2) and equal wildcard counts (0), so the earlier template
    # (id 1) is chosen.
    scores = {
        (r["template_id"], r["revision"]): r for r in tie["scores"]["2"]
    }
    assert scores[(1, 1)]["points"] == 2
    assert scores[(1, 1)]["score"] == "1/2"
    assert scores[(2, 2)]["points"] == 2
    assert scores[(2, 2)]["score"] == "1/2"
    # Both compared against their pre-generalisation revision: 2 points,
    # 0 wildcards each.  Equal -> lower id, so template 1 is selected.
    assert scores[(1, 1)]["wildcards"] == 0
    assert scores[(2, 2)]["wildcards"] == 0
    assert tie["members"]["2"] == [1, 3]
    assert tie["templates"]["1"]["final_render"] == "aa bb <*> <*>"

    # The wildcard tie-break: at threshold 0.75, probe m2 'aa QQ cc dd' ties
    # template 1 (3/4, 0 wildcards) with its own revision 3 (3/4, 1 wildcard);
    # only the fewer-wildcards rule keeps the winner from being a rewrite of
    # itself.  The selected template is still 1.
    wt = data["scenarios"]["wildcard_tie"]
    rows = wt["scores"]["2"]
    rev1 = [r for r in rows if r["template_id"] == 1 and r["revision"] == 1][0]
    rev3 = [r for r in rows if r["template_id"] == 1 and r["revision"] == 3][0]
    # T1 rev1 [aa bb cc dd] vs 'aa QQ cc dd': aa,cc,dd = 3/4 = 0.75.
    assert rev1["points"] == 3 and rev1["score"] == "3/4"
    # T1 rev3 [aa <*> cc dd]: aa,cc,dd = 3 points; the wildcard contributes
    # 0, so still 3/4 -- but with one wildcard instead of zero.
    assert rev3["points"] == 3 and rev3["score"] == "3/4"
    assert rev3["wildcards"] == 1
    assert wt["members"]["2"] == [1, 3]

    masks = data["scenarios"]["masks"]
    scores0 = masks["scores"]["0"]
    # Message 0 'from 10.0.0.1 port 80' creates template 1.
    assert scores0[0]["points"] == 4
    assert scores0[0]["score"] == "1/1"
    # Message 1 'from 7 port 443' vs revision 1
    # [from, <IPV4>, port, <INT>]: from(1), <IPV4> vs <INT>(0), port(1),
    # <INT>(1) = 3/4.
    row_m1 = [r for r in masks["scores"]["1"] if r["revision"] == 1][0]
    assert row_m1["points"] == 3
    assert row_m1["score"] == "3/4"

    literal = data["scenarios"]["literal_wildcard"]
    # Message 2 'pattern x there' vs template 1 revision 2
    # [pattern, LIT(<*>), <*>]: pattern(1), literal '<*>' vs 'x'(0),
    # wildcard(0) = 1/3 < 0.5, so it must create a new template.
    row = [r for r in literal["scores"]["2"]
           if r["template_id"] == 1 and r["revision"] == 2][0]
    assert row["points"] == 1
    assert row["score"] == "1/3"
    assert row["meets_threshold"] is False
    assert literal["members"]["2"] == [2, 3]


def test_transcript_messages_reconstruct_exactly():
    with open(materials.TRANSCRIPT, encoding="utf-8") as fh:
        data = json.load(fh)
    for name, scenario in data["scenarios"].items():
        for row in scenario["message_rows"]:
            words, gaps = ref.split(row["message"])
            rebuilt = gaps[0] + "".join(
                w + gaps[i + 1] for i, w in enumerate(words)
            )
            assert rebuilt == row["message"], (name, row["seq"])
            assert row["reconstructs"] is True


def test_cli_matches_transcript_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        problems = materials.verify_against_cli(tmp)
    assert problems == [], "\n".join(problems)


def test_streams_on_disk_match_scenarios():
    for name, scenario in materials.SCENARIOS.items():
        path = os.path.join(materials.STREAM_DIR, f"{name}.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        assert [line["message"] for line in lines] == scenario["messages"]
        assert [line["seq"] for line in lines] == list(
            range(len(scenario["messages"]))
        )
