from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from logtempl.library import Library, Record  # noqa: E402


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "library.db")


@pytest.fixture()
def make_library(db_path):
    created = []

    def _make(**kwargs):
        kwargs.setdefault("threshold", "0.5")
        lib = Library.create(db_path, **kwargs)
        created.append(lib)
        return lib

    yield _make
    for lib in created:
        try:
            lib.close()
        except Exception:
            pass


def write_stream(path: str, records) -> None:
    """Write ``(source, seq, message)`` records as a UTF-8 JSONL stream."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        for source, seq, message in records:
            handle.write(json.dumps(
                {"source": source, "seq": seq, "message": message},
                ensure_ascii=False,
            ))
            handle.write("\n")


def run_cli(args, env=None, cwd=ROOT):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "logtempl.cli", *args],
        capture_output=True, text=True, env=full_env, cwd=cwd,
    )


def ingest(lib, messages, source="s", start=0):
    return lib.ingest_batch([
        Record(source, start + i, message) for i, message in enumerate(messages)
    ])


CHILD_INGEST = r"""
import sys
from logtempl.library import Library, Record

db, source, start, count = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
messages = sys.stdin.read().split("\x00")
messages = [m for m in messages if m != ""]
lib = Library.open(db)
out = lib.ingest_batch([
    Record(source, start + i, m) for i, m in enumerate(messages[:count])
])
print(f"accepted={len(out.accepted)} created={len(out.templates_created)} "
      f"revised={len(out.templates_revised)}")
lib.close()
"""


def run_child(db, messages, source="s", start=0, env=None):
    """Ingest messages in a child process; return CompletedProcess."""
    payload = "\x00".join(messages)
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", CHILD_INGEST, db, source, str(start), str(len(messages))],
        input=payload, capture_output=True, text=True, env=full_env, cwd=ROOT,
    )


def dump_library(lib: Library, include_tx: bool = True) -> dict:
    """Full structural snapshot used for cross-run equivalence checks.

    ``include_tx=False`` omits transaction identifiers, which legitimately
    depend on how records were grouped into batches; template ids, revision
    numbers, structures, members and texts remain in the comparison.
    """
    templates = {}
    for tid in lib.template_ids():
        head = dict(lib.template_head(tid))
        if not include_tx:
            head.pop("created_tx", None)
        revs = {}
        for rev in lib.template_revisions(tid):
            entry = {
                "render": rev.render,
                "origin": rev.origin,
                "items": [(it.kind, it.value) for it in rev.items],
            }
            if include_tx:
                entry["tx_id"] = rev.tx_id
            revs[rev.revision] = entry
        templates[tid] = {
            "head": head,
            "revisions": revs,
            "members": [
                (m.source, m.seq, m.template_rev, m.message)
                for m in lib.template_members(tid)
            ],
        }
    messages = {}
    for source, seq, _count in lib.source_report():
        for s in range(seq):
            row = lib.get_message(source, s)
            if row is None:
                continue
            messages[(source, s)] = {
                "template_id": row.template_id,
                "template_rev": row.template_rev,
                "word_count": row.word_count,
                "message": row.message,
                "reconstructed": lib.reconstruct_message(source, s),
                "parameters": lib.message_parameters(source, s),
            }
    return {"templates": templates, "messages": messages}
