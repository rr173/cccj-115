"""Independent, naive reference implementation for expected values.

Deliberately not the production matcher.  It re-derives everything from the
written contract with the slowest straightforward code:

* its own character-scanning tokenizer,
* its own mask regexes and hand-written IPv4 range check,
* a linear scan over every template for every message,
* explicit ``Fraction`` arithmetic and a stable sort for the tie-break.

Tests and the ``materials/`` transcript use this module to compute expected
points, scores, selected template ids, revisions and rendered templates.  A
bug in the production index/matcher therefore cannot validate itself.
"""

from __future__ import annotations

import re
from fractions import Fraction

IPV4 = "IPV4"
UUID = "UUID"
INT = "INT"
ALL_MASKS = (IPV4, UUID, INT)

_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_IP_CHARS = re.compile(r"^[0-9.]+$")


def split(message: str):
    """Return ``(words, gaps)``; gaps has one more element than words."""
    words: list[str] = []
    gaps: list[str] = []
    gap: list[str] = []
    word: list[str] = []
    in_word = False
    for ch in message:
        if ch in " \t\n\r\v\f" or ch.isspace():
            if in_word:
                words.append("".join(word))
                word = []
                gaps.append("".join(gap))
                gap = []
                in_word = False
            gap.append(ch)
        else:
            in_word = True
            word.append(ch)
    if in_word:
        words.append("".join(word))
        word = []
        gaps.append("".join(gap))
        gap = []
    gaps.append("".join(gap))
    return words, gaps


def _is_ipv4(word: str) -> bool:
    if not _IP_CHARS.match(word):
        return False
    parts = word.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part or (len(part) > 1 and part[0] == "0"):
            return False
        if int(part) > 255:
            return False
    return True


def classify(word: str, enabled=ALL_MASKS):
    if IPV4 in enabled and _is_ipv4(word):
        return IPV4
    if UUID in enabled and _UUID_RE.match(word):
        return UUID
    if INT in enabled and _INT_RE.match(word):
        return INT
    return None


def item_of(word: str, enabled=ALL_MASKS):
    kind = classify(word, enabled)
    return ("lit", word) if kind is None else ("mask", kind)


def render(items) -> str:
    out = []
    for kind, value in items:
        if kind == "lit":
            out.append("LIT(<*>)" if value == "<*>" else value)
        elif kind == "mask":
            out.append(f"<{value}>")
        else:
            out.append("<*>")
    return " ".join(out)


def score(message_words, template_items, enabled=ALL_MASKS):
    """Return ``(points, wildcards, differing)`` for one aligned pair.

    Accepts template items either as ``(kind, value)`` with ``lit``/``mask``/
    ``wild``, or as ``Item`` objects exposing ``.kind``/``.value`` (translated
    to the same three names).
    """
    tpl = [_normalise_item(t) for t in template_items]
    msg = [item_of(w, enabled) for w in message_words]
    if len(msg) != len(tpl):
        raise ValueError("word counts differ")
    points = wild = differ = 0
    for m, t in zip(msg, tpl):
        if t[0] == "wild":
            wild += 1
        elif m == t:
            points += 1
        else:
            differ += 1
    return points, wild, differ


_ITEM_NAME = {"literal": "lit", "mask": "mask", "wildcard": "wild"}


def _normalise_item(t):
    if hasattr(t, "kind"):
        return (_ITEM_NAME[t.kind], t.value)
    return t


def score_of(message_words, template_items, enabled=ALL_MASKS) -> Fraction:
    points, _wild, _differ = score(message_words, template_items, enabled)
    if not message_words:
        return Fraction(1, 1)
    return Fraction(points, len(message_words))


def merge(template_items, message_words, enabled=ALL_MASKS):
    msg = [item_of(w, enabled) for w in message_words]
    assert len(msg) == len(template_items)
    out = []
    for m, t in zip(msg, template_items):
        if t[0] == "wild" or m == t:
            out.append(t)
        else:
            out.append(("wild", ""))
    return out


def learn(messages, threshold, enabled=ALL_MASKS):
    """Naive full-scan learning.

    Returns ``(templates, revisions, members, final_revision)`` where

    * ``templates`` maps template_id -> final item list,
    * ``revisions`` maps (template_id, revision) -> item list,
    * ``members`` maps message position -> (template_id, revision),
    * revisions increase globally by one per creation/generalisation.
    """
    threshold_f = Fraction(threshold)
    templates: dict[int, list] = {}
    revisions: dict[tuple[int, int], list] = {}
    members: dict[int, tuple[int, int]] = {}
    current_rev: dict[int, int] = {}
    next_id = 1
    next_rev = 1
    for pos, message in enumerate(messages):
        words, _gaps = split(message)
        w = len(words)
        admissible = []
        for tid in sorted(templates):
            items = templates[tid]
            if len(items) != w:
                continue
            points, wild, _differ = score(words, items, enabled)
            frac = Fraction(1, 1) if w == 0 else Fraction(points, w)
            if frac >= threshold_f:
                admissible.append((points, wild, tid))
        # Choose: most points, then fewest wildcards, then smallest id.
        admissible.sort(key=lambda t: (-t[0], t[1], t[2]))
        if not admissible:
            tid = next_id
            next_id += 1
            items = [item_of(word, enabled) for word in words]
            templates[tid] = items
            revisions[(tid, next_rev)] = list(items)
            current_rev[tid] = next_rev
            members[pos] = (tid, next_rev)
            next_rev += 1
            continue
        _points, _wild, tid = admissible[0]
        new_items = merge(templates[tid], words, enabled)
        if new_items != templates[tid]:
            templates[tid] = new_items
            revisions[(tid, next_rev)] = list(new_items)
            current_rev[tid] = next_rev
            members[pos] = (tid, next_rev)
            next_rev += 1
        else:
            members[pos] = (tid, current_rev[tid])
    return templates, revisions, members, next_rev - 1
