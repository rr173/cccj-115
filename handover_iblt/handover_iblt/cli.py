"""Command line entry point.

    handover-iblt build   CHECKLIST DIGEST [--capacity N] [--k K] [--seed S]
                                         [--store STORE]
    handover-iblt info    DIGEST
    handover-iblt compare LEFT_DIGEST RIGHT_DIGEST [--json] [--verbose]
    handover-iblt demo    [--workdir DIR] [--capacity N] [--k K]

``build`` writes a fixed-size digest from one checklist.  ``compare`` needs
only the two digest files — no checklist, no network.  ``demo`` runs the full
handover story end to end.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .builder import build_from_checklist
from .config import (
    DEFAULT_CAPACITY,
    DEFAULT_K,
    DEFAULT_SEED,
    PROTOCOL_VERSION,
    DigestConfig,
)
from .digest import load_digest
from .errors import (
    ChecklistConflictError,
    DigestMismatchError,
    HandoverError,
    UndecodedError,
)
from .reconcile import compare_digests, format_result


def _add_config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--capacity", type=int, default=DEFAULT_CAPACITY,
        help=f"number of IBLT cells (default {DEFAULT_CAPACITY})",
    )
    parser.add_argument(
        "--k", type=int, default=DEFAULT_K,
        help=f"cells touched per element (default {DEFAULT_K})",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED,
        help=f"hash seed (default {DEFAULT_SEED})",
    )


def _cmd_build(args: argparse.Namespace) -> int:
    config = DigestConfig(capacity=args.capacity, k=args.k, seed=args.seed)
    outcome = build_from_checklist(
        args.checklist,
        config,
        store_path=args.store,
        digest_path=args.digest,
        overwrite=args.overwrite,
    )
    print(f"built digest from {args.checklist}")
    for line in outcome.report_lines():
        print(f"  {line}")
    print("  configuration: " + json.dumps(config.as_dict(), sort_keys=True))
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    digest = load_digest(args.digest)
    info = digest.metadata()
    print(f"digest: {args.digest}")
    for key in (
        "protocol",
        "capacity",
        "k",
        "seed",
        "unique_elements",
        "source_lines",
        "duplicate_lines",
        "cell_body_bytes",
        "digest_bytes",
        "nonzero_cells",
        "sum_abs_counts",
        "integrity_sha256",
    ):
        print(f"  {key}: {info[key]}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    left = load_digest(args.left)
    right = load_digest(args.right)
    try:
        result = compare_digests(
            left, right, left_name=args.left_name, right_name=args.right_name
        )
    except (DigestMismatchError, UndecodedError):
        raise
    if args.json:
        print(result.to_json())
    else:
        print(format_result(result, verbose=args.verbose))
    # A non-zero exit status marks an incomplete reconciliation so scripts
    # cannot mistake it for a successful, complete comparison.
    return 0 if result.complete else 3


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(
        workdir=args.workdir,
        capacity=args.capacity,
        k=args.k,
        seed=args.seed,
        quiet=args.quiet,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handover-iblt",
        description=(
            "Reconcile two checklist handovers by exchanging fixed-size "
            "invertible Bloom filter digests (protocol "
            f"{PROTOCOL_VERSION})."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="build a digest from a checklist")
    p_build.add_argument("checklist", help="UTF-8 checklist: UUID,FINGERPRINT")
    p_build.add_argument("digest", help="digest file to create")
    p_build.add_argument("--store", help="SQLite identity store path (optional)")
    p_build.add_argument(
        "--overwrite", action="store_true", help="replace an existing digest"
    )
    _add_config_options(p_build)
    p_build.set_defaults(func=_cmd_build)

    p_info = sub.add_parser("info", help="show digest header and statistics")
    p_info.add_argument("digest")
    p_info.set_defaults(func=_cmd_info)

    p_cmp = sub.add_parser(
        "compare", help="compare two digests and report differences"
    )
    p_cmp.add_argument("left")
    p_cmp.add_argument("right")
    p_cmp.add_argument("--left-name", default="left")
    p_cmp.add_argument("--right-name", default="right")
    p_cmp.add_argument("--json", action="store_true", help="machine-readable output")
    p_cmp.add_argument(
        "--verbose", action="store_true",
        help="also list diagnostic clues when peeling is incomplete",
    )
    p_cmp.set_defaults(func=_cmd_compare)

    p_demo = sub.add_parser(
        "demo", help="run the end-to-end handover story in a work directory"
    )
    p_demo.add_argument("--workdir", required=True, help="empty directory for materials")
    p_demo.add_argument("--quiet", action="store_true", help="only print step headers")
    _add_config_options(p_demo)
    p_demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ChecklistConflictError as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return 4
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except DigestMismatchError as exc:
        print(f"incomparable: {exc}", file=sys.stderr)
        return 5
    except (UndecodedError, HandoverError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
