"""Generate the independent expected-materials transcript and verify it.

Run::

    python3 materials/expected_materials.py --write
    python3 materials/expected_materials.py --check

"Independent" here means: every number in the transcript comes from
:mod:`tests.reference` (a naive full-scan, hand-written implementation of the
contract), never from the production matcher or index.  ``--check`` runs the
real pipeline through the CLI, parses the transcript facts back out of the
executed SQLite database, and fails if any disagree.

The transcript is a deterministic JSON document; ``--write`` refuses if the
file already exists with different content, so a silent expectation rewrite
cannot happen.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TESTS = os.path.join(ROOT, "tests")
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import reference as ref  # noqa: E402

MATERIALS_DIR = os.path.join(ROOT, "materials")
TRANSCRIPT = os.path.join(MATERIALS_DIR, "expected_transcript.json")
STREAM_DIR = os.path.join(MATERIALS_DIR, "streams")

SCENARIOS = {
    "gradual": {
        "threshold": "0.5",
        "depth": 2,
        "masks": "IPV4,UUID,INT",
        "messages": [
            "job 17 started on node a1",
            "job 18 started on node b2",
            "job 19 finished on node b2",
            "job 20 started on node c3",
        ],
    },
    "tie": {
        "threshold": "0.5",
        "depth": 2,
        "masks": "IPV4,UUID,INT",
        "messages": [
            "aa bb cc dd",
            "ee ff gg hh",
            "aa bb gg hh",
        ],
    },
    "wildcard_tie": {
        "threshold": "0.75",
        "depth": 2,
        "masks": "IPV4,UUID,INT",
        "messages": [
            "aa bb cc dd",
            "aa ff cc ee",
            "aa QQ cc dd",
            "aa ff cc dd",
        ],
    },
    "masks": {
        "threshold": "0.5",
        "depth": 2,
        "masks": "IPV4,UUID,INT",
        "messages": [
            "from 10.0.0.1 port 80",
            "from 7 port 443",
            "from 550e8400-e29b-41d4-a716-446655440000 port 8080",
            "peer 256.0.0.1 down",
            "peer 999.1.1.1 down",
        ],
    },
    "literal_wildcard": {
        "threshold": "0.5",
        "depth": 2,
        "masks": "IPV4,UUID,INT",
        "messages": [
            "pattern <*> here",
            "pattern <*> there",
            "pattern x there",
        ],
    },
    "whitespace": {
        "threshold": "1",
        "depth": 2,
        "masks": "IPV4,UUID,INT",
        "messages": [
            "",
            " \t\n ",
            "   ",
            "  aa\tbb  ",
            "aa  bb",
            "aa\tbb",
        ],
    },
    "prefix_relink": {
        "threshold": "0.5",
        "depth": 2,
        "masks": "IPV4,UUID,INT",
        "messages": [
            "alpha beta gamma delta",
            "alpha omega gamma delta",
            "alpha zeta gamma delta",
            "alpha omega gamma delta",
        ],
    },
}


def build_expected():
    out = {"format": "logtempl-expected-materials/1", "scenarios": {}}
    for name in sorted(SCENARIOS):
        scenario = SCENARIOS[name]
        threshold = Fraction(scenario["threshold"])
        templates, revisions, members, final_rev = ref.learn(
            scenario["messages"], threshold
        )
        # Per-message reference view: chosen template, referenced revision,
        # position-by-position parameter values.
        message_rows = []
        for pos, message in enumerate(scenario["messages"]):
            words, gaps = ref.split(message)
            tid, rev = members[pos]
            items = revisions[(tid, rev)]
            params = []
            for k, word in enumerate(words):
                kind = ref.classify(word)
                params.append({
                    "position": k,
                    "kind": "mask" if kind else "literal",
                    "value": kind or word,
                    "word": word,
                })
            message_rows.append({
                "seq": pos,
                "message": message,
                "reconstructs": gaps[0] + "".join(
                    word + gaps[i + 1] for i, word in enumerate(words)
                ) == message,
                "word_count": len(words),
                "gaps": gaps,
                "template_id": tid,
                "template_revision": rev,
                "template_render": ref.render(items),
                "parameters": params,
            })
        # Pairwise score table between each message and each historical
        # revision, so the matching arithmetic is auditable independently.
        scores = {}
        for pos, message in enumerate(scenario["messages"]):
            words, _ = ref.split(message)
            rows = []
            for (tid, rev) in sorted(revisions):
                items = revisions[(tid, rev)]
                if len(items) != len(words):
                    continue
                points, wild, differ = ref.score(words, items)
                frac = Fraction(1, 1) if not words else Fraction(points, len(words))
                rows.append({
                    "template_id": tid,
                    "revision": rev,
                    "points": points,
                    "wildcards": wild,
                    "differing": differ,
                    "score": f"{frac.numerator}/{frac.denominator}",
                    "meets_threshold": frac >= threshold,
                })
            scores[str(pos)] = rows
        out["scenarios"][name] = {
            "threshold": scenario["threshold"],
            "depth": scenario["depth"],
            "masks": scenario["masks"],
            "messages": list(scenario["messages"]),
            "templates": {
                str(tid): {
                    "final_render": ref.render(items),
                    "items": [[kind, value] for kind, value in items],
                }
                for tid, items in sorted(templates.items())
            },
            "revisions": {
                f"{tid}@{rev}": {
                    "render": ref.render(items),
                    "items": [[kind, value] for kind, value in items],
                }
                for (tid, rev), items in sorted(revisions.items())
            },
            "members": {str(pos): [tid, rev] for pos, (tid, rev) in sorted(members.items())},
            "final_revision": final_rev,
            "message_rows": message_rows,
            "scores": scores,
        }
    return out


def write_transcript(path=TRANSCRIPT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    expected = build_expected()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            current = json.load(fh)
        if current != expected:
            raise SystemExit(
                f"refusing to overwrite {path}: independent expectations "
                "changed. Inspect the diff and delete the file deliberately."
            )
        print(f"unchanged {path}")
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(expected, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {path}")


def write_streams():
    os.makedirs(STREAM_DIR, exist_ok=True)
    for name, scenario in SCENARIOS.items():
        path = os.path.join(STREAM_DIR, f"{name}.jsonl")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            for seq, message in enumerate(scenario["messages"]):
                fh.write(json.dumps(
                    {"source": "s", "seq": seq, "message": message},
                    ensure_ascii=False,
                ) + "\n")


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "logtempl.cli", *args],
        cwd=ROOT, capture_output=True, text=True,
    )


def verify_against_cli(tmpdir):
    """Execute every scenario through the CLI and compare to the transcript."""
    with open(TRANSCRIPT, encoding="utf-8") as fh:
        expected = json.load(fh)
    problems = []
    for name in sorted(expected["scenarios"]):
        scenario = expected["scenarios"][name]
        db = os.path.join(tmpdir, f"{name}.db")
        stream = os.path.join(STREAM_DIR, f"{name}.jsonl")
        result = _run_cli(
            "create", db, "--threshold", scenario["threshold"],
            "--depth", str(scenario["depth"]), "--masks", scenario["masks"],
        )
        if result.returncode != 0:
            problems.append(f"{name}: create failed: {result.stderr}")
            continue
        result = _run_cli("ingest", db, stream)
        if result.returncode != 0:
            problems.append(f"{name}: ingest failed: {result.stderr}")
            continue
        # Read back every message and template through the CLI and compare.
        for seq, row in enumerate(scenario["message_rows"]):
            show = _run_cli("show-message", db, "s", str(seq), "--json")
            data = json.loads(show.stdout)
            if data["message"] != row["message"]:
                problems.append(f"{name} seq {seq}: message text differs")
            if data["reconstructed"] != row["message"]:
                problems.append(f"{name} seq {seq}: reconstruction differs")
            if not data["exact"]:
                problems.append(f"{name} seq {seq}: reconstruction not exact")
            if data["template_id"] != row["template_id"]:
                problems.append(
                    f"{name} seq {seq}: template id {data['template_id']} "
                    f"!= expected {row['template_id']}"
                )
            if data["template_revision"] != row["template_revision"]:
                problems.append(
                    f"{name} seq {seq}: revision {data['template_revision']} "
                    f"!= expected {row['template_revision']}"
                )
            got_params = [
                (p["position"], p["kind"], p["value"], p["word"])
                for p in data["parameters"]
            ]
            want_params = [
                (p["position"], p["kind"], p["value"], p["word"])
                for p in row["parameters"]
            ]
            if got_params != want_params:
                problems.append(f"{name} seq {seq}: parameters differ")
        for tid, tpl in scenario["templates"].items():
            show = _run_cli("show-template", db, tid, "--history")
            if show.returncode != 0:
                problems.append(f"{name}: show-template {tid} failed")
                continue
            for rev, revdata in scenario["revisions"].items():
                rev_tid, rev_no = rev.split("@")
                if rev_tid != tid:
                    continue
                one = _run_cli("show-template", db, tid, "--revision", rev_no)
                if f"structure: {revdata['render']}" not in one.stdout:
                    problems.append(
                        f"{name}: template {tid} revision {rev_no} render "
                        f"differs"
                    )
        verify = _run_cli("verify", db)
        if verify.returncode != 0:
            problems.append(f"{name}: verify failed: {verify.stderr}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="write transcript and input streams")
    parser.add_argument("--check", action="store_true",
                        help="run the pipeline through the CLI and verify")
    args = parser.parse_args(argv)
    if not args.write and not args.check:
        parser.error("choose --write or --check")
    if args.write:
        write_transcript()
        write_streams()
        print(f"wrote {len(SCENARIOS)} input streams under {STREAM_DIR}")
    if args.check:
        if not os.path.exists(TRANSCRIPT):
            raise SystemExit(f"missing {TRANSCRIPT}; run --write first")
        with tempfile.TemporaryDirectory() as tmp:
            problems = verify_against_cli(tmp)
        if problems:
            print("MISMATCHES:")
            for problem in problems:
                print(" -", problem)
            return 1
        print("all scenarios match the independent transcript")
    return 0


if __name__ == "__main__":
    sys.exit(main())
