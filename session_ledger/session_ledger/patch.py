"""JSONL patch parsing and strict structural validation.

A patch is a UTF-8 JSONL file.  Every line is one JSON object::

    {"op": "put",     "event_id": "e1", "revision": 3,
     "user_id": "u1", "event_ms": 1200}
    {"op": "retract", "event_id": "e1", "revision": 4}

Validation performed here (structural, without touching the database):

* every line must parse as one JSON object with no duplicated object keys;
* ``op`` is exactly ``"put"`` or ``"retract"``;
* ``event_id`` is a non-empty string, compared byte-for-byte (no case folding);
* ``revision`` is a positive integer -- booleans are not integers;
* ``put`` additionally requires non-empty string ``user_id`` and a 64-bit
  signed integer ``event_ms``;
* ``retract`` must not carry ``user_id`` / ``event_ms``;
* an ``event_id`` may appear at most once in one patch;
* unknown / missing / wrongly typed fields point at the physical line and the
  whole patch is refused.

Revision-order decisions (stale / conflict / no-op) need stored state and are
made by :mod:`session_ledger.store`, not here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PatchError
from .schema import INT64_MAX, INT64_MIN


@dataclass(frozen=True)
class ParsedOp:
    """One validated patch line."""

    line: int
    op: str
    event_id: str
    revision: int
    user_id: str | None = None
    event_ms: int | None = None


def _is_positive_int(value: Any) -> bool:
    # bool is a subclass of int; the domain explicitly excludes booleans.
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def _validate_object(line_no: int, obj: Any) -> ParsedOp:
    if not isinstance(obj, dict):
        raise PatchError("patch line must be a JSON object", line_no)

    op = obj.get("op")
    if op not in ("put", "retract"):
        raise PatchError("field 'op' must be 'put' or 'retract'", line_no)

    event_id = obj.get("event_id")
    if not _is_nonempty_str(event_id):
        raise PatchError("field 'event_id' must be a non-empty string", line_no)

    revision = obj.get("revision")
    if not _is_positive_int(revision):
        raise PatchError("field 'revision' must be a positive integer", line_no)

    allowed = {"op", "event_id", "revision"}
    if op == "put":
        allowed |= {"user_id", "event_ms"}
        user_id = obj.get("user_id")
        if not _is_nonempty_str(user_id):
            raise PatchError(
                "put requires non-empty string field 'user_id'", line_no
            )
        event_ms = obj.get("event_ms")
        if not (isinstance(event_ms, int) and not isinstance(event_ms, bool)):
            raise PatchError(
                "put requires integer field 'event_ms'", line_no
            )
        if not (INT64_MIN <= event_ms <= INT64_MAX):
            raise PatchError(
                "field 'event_ms' must be a signed 64-bit integer", line_no
            )
    else:
        if "user_id" in obj or "event_ms" in obj:
            raise PatchError(
                "retract must not carry 'user_id' or 'event_ms'", line_no
            )
        user_id = None
        event_ms = None

    extra = set(obj) - allowed
    if extra:
        raise PatchError(
            f"unexpected field(s): {', '.join(sorted(extra))}", line_no
        )

    return ParsedOp(
        line=line_no,
        op=op,
        event_id=event_id,
        revision=revision,
        user_id=user_id,
        event_ms=event_ms,
    )


class _DuplicateKeyDetector:
    """``object_pairs_hook`` that rejects repeated keys inside one object."""

    def __call__(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"duplicate JSON object key {key!r}")
            seen.add(key)
        return dict(pairs)


def parse_patch(path: str | Path) -> list[ParsedOp]:
    """Parse and structurally validate a JSONL patch file.

    Returns the ordered operations (empty list for an empty file).  Raises
    :class:`PatchError` pointing at the physical line on any violation.
    """
    detector = _DuplicateKeyDetector()
    ops: list[ParsedOp] = []
    seen_ids: set[str] = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            if not raw.strip():
                # Blank lines carry no object and are therefore malformed.
                raise PatchError("blank line is not a valid patch object", line_no)
            try:
                obj = json.loads(raw, object_pairs_hook=detector)
            except json.JSONDecodeError as exc:
                raise PatchError(f"invalid JSON: {exc.msg}", line_no) from exc
            except ValueError as exc:
                raise PatchError(str(exc), line_no) from exc

            parsed = _validate_object(line_no, obj)
            if parsed.event_id in seen_ids:
                raise PatchError(
                    f"event_id {parsed.event_id!r} appears twice in this patch",
                    line_no,
                )
            seen_ids.add(parsed.event_id)
            ops.append(parsed)
    return ops
