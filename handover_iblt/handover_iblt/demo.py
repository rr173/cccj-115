"""Run the documented handover story end to end.

Every step below is executed by calling the real command line in a
subprocess; nothing is simulated.  The demo prints the commands, their output
and a short interpretation, and returns a non-zero status if any expectation
does not hold.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from . import materials as M
from .config import DEFAULT_K, DEFAULT_SEED, DigestConfig


def _run(workdir: str, args: list[str], *, expect: int = 0,
         quiet: bool = False) -> str:
    cmd = [sys.executable, "-m", "handover_iblt.cli", *args]
    env = {**os.environ, "PYTHONPATH": _package_root()}
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, check=False, env=env
    )
    if not quiet:
        print("$ " + " ".join(cmd[2:]))
    output = (proc.stdout + proc.stderr).rstrip()
    if output and not quiet:
        print(output)
    if proc.returncode != expect:
        raise SystemExit(
            f"demo step failed: {' '.join(cmd)} -> {proc.returncode} "
            f"(expected {expect})\n{output}"
        )
    return proc.stdout


def _package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"demo expectation failed: {message}")
    print(f"  ok: {message}")


def _compare_json(workdir: str, left: str, right: str, *,
                  left_name: str, right_name: str) -> dict:
    stdout = _run(
        workdir,
        [
            "compare", left, right,
            "--left-name", left_name, "--right-name", right_name, "--json",
        ],
    )
    return json.loads(stdout)


def run_demo(workdir: str, *, capacity: int, k: int, seed: int,
             quiet: bool = False) -> int:
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    cfg = DigestConfig(capacity=capacity, k=k, seed=seed)
    cfg_args = ["--capacity", str(capacity), "--k", str(k), "--seed", str(seed)]

    _section("1. Sender has A, B, C; receiver has B, C, D")
    story = M.story_main(workdir)
    _run(workdir, ["build", story["left"], "sender.digest", *cfg_args])
    _run(workdir, ["build", story["right"], "receiver.digest", *cfg_args])
    _run(workdir, ["info", "sender.digest"])
    result = _compare_json(
        workdir, "sender.digest", "receiver.digest",
        left_name="sender", right_name="receiver",
    )
    _run(
        workdir,
        ["compare", "sender.digest", "receiver.digest",
         "--left-name", "sender", "--right-name", "receiver"],
    )
    _check(result["complete"] is True, "reconciliation completed")
    kinds = {(d["kind"], d["uuid"]) for d in result["differences"]}
    _check(
        ("LEFT_ONLY", M.A) in kinds,
        "A is reported LEFT_ONLY (sender only)",
    )
    _check(
        ("RIGHT_ONLY", M.D) in kinds,
        "D is reported RIGHT_ONLY (receiver only)",
    )
    _check(
        len(result["differences"]) == 2,
        "exactly two differences are reported",
    )

    _section("2. Content changed: C has a different fingerprint on each side")
    changed = M.story_changed(workdir)
    _run(workdir, ["build", changed["left"], "chg_sender.digest", *cfg_args])
    _run(workdir, ["build", changed["right"], "chg_receiver.digest", *cfg_args])
    changed_result = _compare_json(
        workdir, "chg_sender.digest", "chg_receiver.digest",
        left_name="sender", right_name="receiver",
    )
    _run(
        workdir,
        ["compare", "chg_sender.digest", "chg_receiver.digest",
         "--left-name", "sender", "--right-name", "receiver"],
    )
    changed_rows = [
        d for d in changed_result["differences"]
        if d["kind"] == "CONTENT_CHANGED" and d["uuid"] == M.C
    ]
    _check(len(changed_rows) == 1, "C is one CONTENT_CHANGED row, not two")
    _check(
        changed_rows[0]["left_fingerprint"] == M.FP_C,
        "the sender fingerprint of C is kept",
    )
    _check(
        changed_rows[0]["right_fingerprint"] == M.FP_C2,
        "the receiver fingerprint of C is kept",
    )

    _section("3. Swapping the two sides reverses the direction")
    forward = _compare_json(
        workdir, "sender.digest", "receiver.digest",
        left_name="sender", right_name="receiver",
    )
    reverse = _compare_json(
        workdir, "receiver.digest", "sender.digest",
        left_name="receiver", right_name="sender",
    )
    _run(
        workdir,
        ["compare", "receiver.digest", "sender.digest",
         "--left-name", "receiver", "--right-name", "sender"],
    )
    forward_kinds = {(d["kind"], d["uuid"]) for d in forward["differences"]}
    reverse_kinds = {(d["kind"], d["uuid"]) for d in reverse["differences"]}
    _check(
        forward_kinds == {("LEFT_ONLY", M.A), ("RIGHT_ONLY", M.D)},
        "forward: A LEFT_ONLY, D RIGHT_ONLY",
    )
    _check(
        reverse_kinds == {("RIGHT_ONLY", M.A), ("LEFT_ONLY", M.D)},
        "reverse: A RIGHT_ONLY, D LEFT_ONLY",
    )
    changed_reverse = [
        d for d in reverse["differences"]
        if d["kind"] == "CONTENT_CHANGED"
    ]
    _check(
        not changed_reverse,
        "the unchanged pair has no rows in this comparison",
    )

    _section("4. Identical content, different order and duplicated lines")
    same = M.story_same(workdir)
    _run(workdir, ["build", same["left"], "same_left.digest", *cfg_args])
    _run(workdir, ["build", same["right"], "same_right.digest", *cfg_args])
    same_result = _compare_json(
        workdir, "same_left.digest", "same_right.digest",
        left_name="sender", right_name="receiver",
    )
    _run(
        workdir,
        ["compare", "same_left.digest", "same_right.digest",
         "--left-name", "sender", "--right-name", "receiver"],
    )
    _check(same_result["complete"] is True, "reconciliation completed")
    _check(
        same_result["differences"] == [],
        "empty difference list despite reordering and duplicate lines",
    )

    _section("5. Repeated lines and a same-identifier conflict")
    dup_path = M.story_duplicates(workdir)
    _run(workdir, ["build", dup_path, "dups.digest", *cfg_args])
    conflict_path = M.story_conflict(workdir)
    _run(workdir, ["build", conflict_path, "conflict.digest", *cfg_args],
         expect=4)
    _check(
        not os.path.exists(os.path.join(workdir, "conflict.digest")),
        "no digest is written for a conflicting checklist",
    )

    _section("6. Malformed hexadecimal input")
    for label, path in M.story_malformed(workdir).items():
        print(f"-- {label}")
        _run(
            workdir,
            ["build", path, f"bad_{label}.digest", *cfg_args],
            expect=1,
        )
        _check(
            not os.path.exists(os.path.join(workdir, f"bad_{label}.digest")),
            f"no digest is written for {label}",
        )

    _section("7. Incomparable configurations")
    _run(
        workdir,
        ["build", story["left"], "sender_wide.digest",
         "--capacity", str(capacity * 2), "--k", str(k), "--seed", str(seed)],
    )
    digest_path = os.path.join(workdir, "sender.digest")
    before = os.stat(digest_path)
    _run(
        workdir,
        ["compare", "sender.digest", "sender_wide.digest"],
        expect=5,
    )
    after = os.stat(digest_path)
    _check(
        (before.st_size, before.st_mtime_ns)
        == (after.st_size, after.st_mtime_ns)
        and after.st_size == 96 + 64 * capacity,
        "the original digest files are untouched by the failed comparison",
    )

    _section("8. A residual cycle that cannot be peeled, then a larger table")
    cycle = M.story_cycle(workdir)
    cycle_args = [
        "--capacity", str(M.CYCLE_CAPACITY),
        "--k", str(M.CYCLE_K), "--seed", str(M.CYCLE_SEED),
    ]
    _run(workdir, ["build", cycle["left"], "cycle_left.digest", *cycle_args])
    _run(workdir, ["build", cycle["right"], "cycle_right.digest", *cycle_args])
    _run(
        workdir,
        ["compare", "cycle_left.digest", "cycle_right.digest",
         "--left-name", "sender", "--right-name", "receiver"],
        expect=3,
    )
    # The retry must rebuild from the original checklists: only the
    # capacity changes, and the checklist files are still available.
    retry_args = [
        "--capacity", str(M.CYCLE_CAPACITY_RETRY),
        "--k", str(M.CYCLE_K), "--seed", str(M.CYCLE_SEED),
    ]
    _run(workdir, ["build", cycle["left"], "cycle_left_retry.digest", *retry_args])
    _run(workdir, ["build", cycle["right"], "cycle_right_retry.digest", *retry_args])
    retry_result = _compare_json(
        workdir, "cycle_left_retry.digest", "cycle_right_retry.digest",
        left_name="sender", right_name="receiver",
    )
    _run(
        workdir,
        ["compare", "cycle_left_retry.digest", "cycle_right_retry.digest",
         "--left-name", "sender", "--right-name", "receiver"],
    )
    _check(retry_result["complete"] is True, "the larger table peels completely")
    retry_kinds = {(d["kind"], d["uuid"]) for d in retry_result["differences"]}
    _check(
        ("LEFT_ONLY", M.P_UUID) in retry_kinds
        and ("RIGHT_ONLY", M.Q_UUID) in retry_kinds,
        "P and Q are reported with the right direction",
    )

    _section("9. Digest size is fixed; it does not grow with the checklist")
    sizes = []
    for n in (3, 500, 5000):
        rows = [
            (i.to_bytes(16, "big").hex(), os.urandom(32).hex())
            for i in range(n)
        ]
        path = os.path.join(workdir, f"scale_{n}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            for uuid_hex, fp_hex in rows:
                fh.write(f"{uuid_hex},{fp_hex}\n")
        _run(
            workdir,
            ["build", path, f"scale_{n}.digest", *cfg_args],
            quiet=True,
        )
        size = os.path.getsize(os.path.join(workdir, f"scale_{n}.digest"))
        sizes.append((n, size))
        print(f"  {n:>5} checklist rows -> {size} byte digest")
    _check(
        len({size for _, size in sizes}) == 1,
        "all three digests have the same size",
    )
    _check(
        sizes[0][1] == 96 + 64 * capacity,
        "digest size equals 96 + 64 * capacity bytes",
    )

    _section("Demo complete")
    print("Every documented expectation held.")
    return 0
