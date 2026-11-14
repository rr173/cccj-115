"""Command line interface for the log-template pipeline.

Commands
--------
``create``          create an empty library (threshold, depth, caches, masks).
``ingest``          feed a UTF-8 JSONL message stream into the library.
``show-message``    show one accepted record and its reconstructed text.
``parameters``      show per-position parameters of one accepted record.
``list-templates``  list every template with revision and counts.
``show-template``   show a template's current or historical structure.
``members``         list the accepted messages of a template.
``stats``           library counters, per-source positions and cache stats.
``verify``          run structural integrity checks.
``rebuild-index``   recompute the derived candidate index from templates.

Exit codes: 0 success, 2 usage/library error, 3 gap refusal, 4 conflict,
5 input format error.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError
from .library import (
    ConflictError,
    GapError,
    InputFormatError,
    Library,
    LibraryExists,
    LogTemplateError,
    Record,
    UnknownTemplate,
)
from .masks import SUPPORTED_MASK_KINDS

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GAP = 3
EXIT_CONFLICT = 4
EXIT_INPUT = 5


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _read_stream(path: str):
    """Yield validated :class:`Record` objects from a UTF-8 JSONL file.

    Blank lines are ignored (they carry no record); every other line must be
    one JSON object with string ``source``, integer ``seq`` and string
    ``message``.  The message is taken verbatim -- no trimming, no newline
    stripping beyond the JSON line terminator itself.
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for line_no, line in enumerate(handle, 1):
            text = line.rstrip("\n")
            if text.endswith("\r"):
                text = text[:-1]
            if text.strip() == "":
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InputFormatError(
                    f"{path}:{line_no}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(obj, dict):
                raise InputFormatError(f"{path}:{line_no}: object expected")
            for key in ("source", "seq", "message"):
                if key not in obj:
                    raise InputFormatError(
                        f"{path}:{line_no}: missing field {key!r}"
                    )
            source, seq, message = obj["source"], obj["seq"], obj["message"]
            if not isinstance(source, str) or source == "":
                raise InputFormatError(
                    f"{path}:{line_no}: source must be a non-empty string"
                )
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise InputFormatError(
                    f"{path}:{line_no}: seq must be a non-negative integer"
                )
            if not isinstance(message, str):
                raise InputFormatError(
                    f"{path}:{line_no}: message must be a string"
                )
            yield line_no, Record(source, seq, message)


def _cmd_create(args: argparse.Namespace) -> int:
    try:
        Library.create(
            args.database,
            threshold=args.threshold,
            depth=args.depth,
            template_cache=args.template_cache,
            node_cache=args.node_cache,
            enabled_masks=args.masks,
        )
    except (LibraryExists, ConfigError) as exc:
        _err(str(exc))
        return EXIT_USAGE
    print(f"created library {args.database}")
    return EXIT_OK


def _cmd_ingest(args: argparse.Namespace) -> int:
    total = 0
    replays = 0
    created = revised = 0
    visited = examined = 0
    try:
        lib = Library.open(args.database)
    except (LogTemplateError, ConfigError, FileNotFoundError) as exc:
        _err(str(exc))
        return EXIT_USAGE
    with lib:
        batch: list[Record] = []
        try:
            for _line_no, record in _read_stream(args.input):
                batch.append(record)
                if args.batch_size and len(batch) >= args.batch_size:
                    total, replays, created, revised, visited, examined = _run_batch(
                        lib, batch, args.token, total, replays, created,
                        revised, visited, examined,
                    )
                    batch = []
            if batch:
                total, replays, created, revised, visited, examined = _run_batch(
                    lib, batch, args.token, total, replays, created, revised,
                    visited, examined,
                )
        except GapError as exc:
            _err(f"batch refused (seq gap): {exc}")
            return EXIT_GAP
        except ConflictError as exc:
            _err(f"batch refused (conflict): {exc}")
            return EXIT_CONFLICT
        except InputFormatError as exc:
            _err(str(exc))
            return EXIT_INPUT
        except LogTemplateError as exc:
            _err(str(exc))
            return EXIT_USAGE
    print(
        f"accepted={total} replays_ignored={replays} "
        f"templates_created={created} templates_revised={revised} "
        f"index_nodes_visited={visited} candidates_examined={examined}"
    )
    return EXIT_OK


def _run_batch(lib, batch, token, total, replays, created, revised, visited,
               examined):
    outcome = lib.ingest_batch(batch, token=token)
    total += len(outcome.accepted)
    replays += len(outcome.ignored_replays)
    created += len(outcome.templates_created)
    revised += len(outcome.templates_revised)
    visited += outcome.index_nodes_visited
    examined += outcome.candidates_examined
    return total, replays, created, revised, visited, examined


def _cmd_show_message(args: argparse.Namespace) -> int:
    with Library.open(args.database) as lib:
        row = lib.get_message(args.source, args.seq)
        if row is None:
            _err(f"no message at {args.source!r}:{args.seq}")
            return EXIT_USAGE
        rebuilt = lib.reconstruct_message(args.source, args.seq)
        if args.json:
            print(json.dumps({
                "source": row.source,
                "seq": row.seq,
                "template_id": row.template_id,
                "template_revision": row.template_rev,
                "word_count": row.word_count,
                "message": row.message,
                "reconstructed": rebuilt,
                "exact": rebuilt == row.message,
                "parameters": [
                    {"position": p, "kind": k, "value": v, "word": w}
                    for p, k, v, w in lib.message_parameters(
                        args.source, args.seq
                    )
                ],
            }, ensure_ascii=False, indent=2))
        else:
            print(f"source: {row.source}")
            print(f"seq: {row.seq}")
            print(f"template: {row.template_id} revision {row.template_rev}")
            print(f"words: {row.word_count}")
            print(f"message: {row.message!r}")
            print(f"reconstructed: {rebuilt!r}")
            print(f"exact: {rebuilt == row.message}")
        return EXIT_OK


def _cmd_parameters(args: argparse.Namespace) -> int:
    with Library.open(args.database) as lib:
        if lib.get_message(args.source, args.seq) is None:
            _err(f"no message at {args.source!r}:{args.seq}")
            return EXIT_USAGE
        for pos, kind, value, word in lib.message_parameters(
            args.source, args.seq
        ):
            if kind == "mask":
                print(f"{pos}\tmask\t{value}\t{word}")
            else:
                print(f"{pos}\tliteral\t-\t{word}")
    return EXIT_OK


def _fmt_items(items) -> str:
    return " ".join(item.render() for item in items)


def _cmd_list_templates(args: argparse.Namespace) -> int:
    with Library.open(args.database) as lib:
        ids = lib.template_ids()
        if not ids:
            print("(no templates)")
            return EXIT_OK
        print("id\twords\trev\twildcards\tmembers\ttotal\tstructure")
        for tid in ids:
            head = lib.template_head(tid)
            print(
                f"{tid}\t{head['word_count']}\t{head['revision']}\t"
                f"{head['wildcards']}\t{head['member_count']}\t"
                f"{head['total_count']}\t{head['render']}"
            )
    return EXIT_OK


def _cmd_show_template(args: argparse.Namespace) -> int:
    with Library.open(args.database) as lib:
        if args.revision is None:
            head = lib.template_head(args.template_id)
            revision = head["revision"]
            print(
                f"template {args.template_id} current revision {revision} "
                f"(words={head['word_count']} wildcards={head['wildcards']} "
                f"members={head['member_count']} total={head['total_count']})"
            )
            info = lib.template_revision(args.template_id, revision)
        else:
            revision = args.revision
            info = lib.template_revision(args.template_id, revision)
            print(
                f"template {args.template_id} revision {revision} "
                f"(words={info.word_count} wildcards={info.wildcards} "
                f"origin={info.origin} tx={info.tx_id})"
            )
        print(f"structure: {_fmt_items(info.items)}")
        for pos, item in enumerate(info.items):
            print(f"  {pos}\t{item.kind}\t{item.value!r}")
        if args.revision is None and args.history:
            print("history:")
            for rev in lib.template_revisions(args.template_id):
                print(f"  rev {rev.revision} [{rev.origin}] {rev.render}")
    return EXIT_OK


def _cmd_members(args: argparse.Namespace) -> int:
    with Library.open(args.database) as lib:
        try:
            members = lib.template_members(args.template_id, args.revision)
        except UnknownTemplate as exc:
            _err(str(exc))
            return EXIT_USAGE
        for m in members:
            print(f"{m.source}\t{m.seq}\trev{m.template_rev}\t{m.message!r}")
        if not members:
            print("(no members)")
    return EXIT_OK


def _cmd_stats(args: argparse.Namespace) -> int:
    with Library.open(args.database) as lib:
        print("config:")
        for key, value in lib.config_report().items():
            print(f"  {key}: {value}")
        print("sources:")
        for source, next_seq, count in lib.source_report():
            print(f"  {source}\tnext_seq={next_seq}\tmessages={count}")
        print(f"templates: {len(lib.template_ids())}")
        print(f"messages: {lib.message_count()}")
        print("caches:")
        print(json.dumps(lib.cache_stats(), sort_keys=True))
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    with Library.open(args.database) as lib:
        try:
            lib.verify_integrity()
        except LogTemplateError as exc:
            _err(str(exc))
            return EXIT_USAGE
        print("integrity: ok")
    return EXIT_OK


def _cmd_rebuild_index(args: argparse.Namespace) -> int:
    with Library.open(args.database) as lib:
        lib.rebuild_index()
        lib.verify_integrity()
        print("index rebuilt and verified")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logtempl",
        description="Structure log messages into templates and parameters.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="create a new library")
    p.add_argument("database")
    p.add_argument("--threshold", default="0.5",
                   help="exact decimal in (0,1], default 0.5")
    p.add_argument("--depth", type=int, default=2,
                   help="index depth (non-negative), default 2")
    p.add_argument("--template-cache", type=int, default=64)
    p.add_argument("--node-cache", type=int, default=256)
    p.add_argument("--masks", default=None,
                   help="comma list of enabled rules from "
                        f"{','.join(SUPPORTED_MASK_KINDS)} (default all)")
    p.set_defaults(func=_cmd_create)

    p = sub.add_parser("ingest", help="feed a UTF-8 JSONL stream")
    p.add_argument("database")
    p.add_argument("input")
    p.add_argument("--batch-size", type=int, default=0,
                   help="records per atomic batch (0 = whole file)")
    p.add_argument("--token", default=None, help="optional batch label")
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("show-message")
    p.add_argument("database")
    p.add_argument("source")
    p.add_argument("seq", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_show_message)

    p = sub.add_parser("parameters")
    p.add_argument("database")
    p.add_argument("source")
    p.add_argument("seq", type=int)
    p.set_defaults(func=_cmd_parameters)

    p = sub.add_parser("list-templates")
    p.add_argument("database")
    p.set_defaults(func=_cmd_list_templates)

    p = sub.add_parser("show-template")
    p.add_argument("database")
    p.add_argument("template_id", type=int)
    p.add_argument("--revision", type=int, default=None)
    p.add_argument("--history", action="store_true")
    p.set_defaults(func=_cmd_show_template)

    p = sub.add_parser("members")
    p.add_argument("database")
    p.add_argument("template_id", type=int)
    p.add_argument("--revision", type=int, default=None)
    p.set_defaults(func=_cmd_members)

    p = sub.add_parser("stats")
    p.add_argument("database")
    p.set_defaults(func=_cmd_stats)

    p = sub.add_parser("verify")
    p.add_argument("database")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("rebuild-index")
    p.add_argument("database")
    p.set_defaults(func=_cmd_rebuild_index)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (LogTemplateError, ConfigError, FileNotFoundError) as exc:
        _err(str(exc))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
