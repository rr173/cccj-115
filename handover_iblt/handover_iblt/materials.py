"""Build demo materials with hand-written expected values.

All checklist bytes used by ``docs/DEMO.md`` are produced here from literal
hex constants, so the README and the demonstration share one source and the
expected cells in ``tests/expected.py`` are independent of the production
code.
"""

from __future__ import annotations

import hashlib
import os

#: Stable, readable demo identifiers.  UUIDs are 16 bytes, fingerprints 32.
A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
C = "cccccccccccccccccccccccccccccccc"
D = "dddddddddddddddddddddddddddddddd"
E = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


def fp(label: str) -> str:
    """Deterministic content fingerprint distinct per label."""
    return hashlib.sha256(b"specimen:" + label.encode()).hexdigest()


FP_A = fp("A")
FP_B = fp("B")
FP_C = fp("C")
FP_D = fp("D")
FP_E = fp("E")
FP_C2 = fp("C-revised")


def line(uuid_hex: str, fingerprint_hex: str) -> str:
    return f"{uuid_hex},{fingerprint_hex}"


def write(path: str, rows: list[str]) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for row in rows:
            fh.write(row)
            fh.write("\n")
    return path


# -- the main handover story ------------------------------------------------
# sender: A, B, C   receiver: B, C, D

def story_main(workdir: str) -> dict:
    left = write(
        os.path.join(workdir, "sender.txt"),
        [line(A, FP_A), line(B, FP_B), line(C, FP_C)],
    )
    right = write(
        os.path.join(workdir, "receiver.txt"),
        [line(B, FP_B), line(C, FP_C), line(D, FP_D)],
    )
    return {"left": left, "right": right}


# -- same elements, different order and duplicated lines --------------------

def story_same(workdir: str) -> dict:
    left = write(
        os.path.join(workdir, "same_left.txt"),
        [line(A, FP_A), line(B, FP_B), line(C, FP_C), line(A, FP_A)],
    )
    right = write(
        os.path.join(workdir, "same_right.txt"),
        [line(C, FP_C), line(A, FP_A), line(B, FP_B), line(B, FP_B)],
    )
    return {"left": left, "right": right}


# -- one content change + one-sided additions ------------------------------

def story_changed(workdir: str) -> dict:
    left = write(
        os.path.join(workdir, "changed_left.txt"),
        [line(A, FP_A), line(B, FP_B), line(C, FP_C)],
    )
    right = write(
        os.path.join(workdir, "changed_right.txt"),
        [line(B, FP_B), line(C, FP_C2), line(D, FP_D)],
    )
    return {"left": left, "right": right}


# -- deliberately broken materials ------------------------------------------

def story_duplicates(workdir: str) -> str:
    return write(
        os.path.join(workdir, "duplicates.txt"),
        [
            line(A, FP_A),
            line(B, FP_B),
            line(A, FP_A),
            line(B, FP_B),
            line(A, FP_A),
        ],
    )


def story_conflict(workdir: str) -> str:
    return write(
        os.path.join(workdir, "conflict.txt"),
        [
            line(A, FP_A),
            line(B, FP_B),
            line(C, FP_C),
            line(A, FP_C2),
            line(D, FP_D),
        ],
    )


def story_malformed(workdir: str) -> dict:
    paths = {}
    paths["not_hex"] = write(
        os.path.join(workdir, "bad_not_hex.txt"),
        [line(A, FP_A), line(B, "zz" + FP_B[2:])],
    )
    paths["short_uuid"] = write(
        os.path.join(workdir, "bad_short_uuid.txt"),
        [line(A[:30], FP_A)],
    )
    paths["short_fp"] = write(
        os.path.join(workdir, "bad_short_fp.txt"),
        [line(A, FP_A[:62])],
    )
    paths["three_fields"] = write(
        os.path.join(workdir, "bad_three_fields.txt"),
        [f"{A},{FP_A},{B}"],
    )
    return paths


# -- residual cycle material ------------------------------------------------
#
# With capacity=16, k=2, seed=5 the two identifiers below hash to exactly the
# same pair of cells, [12, 13].  A shared set of eight entries is placed on
# both sides; the sender then adds P, the receiver adds Q.  The only two
# differing cells each hold {+P, -Q}: abs(count) is 0 or 2, never 1, so
# peeling cannot start.  A capacity of 64 separates P and Q.

CYCLE_SEED = 5
CYCLE_CAPACITY = 16
CYCLE_CAPACITY_RETRY = 64
CYCLE_K = 2


def _cycle_element(number: int) -> tuple[str, str]:
    uuid_hex = number.to_bytes(16, "big").hex()
    fingerprint_hex = bytes([number % 251]) * 32
    return uuid_hex, fingerprint_hex.hex()


P_UUID, P_FP = _cycle_element(63)
Q_UUID, Q_FP = _cycle_element(181)
COMMON = [_cycle_element(i) for i in range(1, 9)]


def story_cycle(workdir: str) -> dict:
    left = write(
        os.path.join(workdir, "cycle_left.txt"),
        [line(u, f) for u, f in COMMON + [(P_UUID, P_FP)]],
    )
    right = write(
        os.path.join(workdir, "cycle_right.txt"),
        [line(u, f) for u, f in COMMON + [(Q_UUID, Q_FP)]],
    )
    return {"left": left, "right": right}
