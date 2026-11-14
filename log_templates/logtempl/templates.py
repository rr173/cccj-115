"""Template items, scoring and tie-break rules.

A template is a fixed-length sequence of items, one per word position.

Item kinds
    ``literal``   exact word text.  Only the identical word earns a point.
    ``mask``      a whole word masked as IPV4 / UUID / INT.  Only a word
                  masked to the *same* kind earns a point.
    ``wildcard``  learned generalization.  Any word may occupy the position;
                  it never earns a point.

Match score
    With a message of ``W`` words and a template of ``W`` items::

        points = number of positions where the message item and the template
                 item are both fixed and equal (same literal text, or the same
                 mask kind)
        score  = points / W                      (W > 0)
        score  = 1                                (W == 0, by definition)

    Every position where the items differ -- literal vs another literal,
    mask vs literal, mask vs a different mask kind, or a template wildcard --
    contributes 0.  A candidate is admissible when ``score >= threshold``.
    Differing fixed positions are *not* by themselves disqualifying: on
    acceptance they are generalized to wildcards.

Selection among admissible candidates with equal ``points`` (equal score at
a fixed W):
    1. fewer template wildcards;
    2. lower ``template_id`` (creation order, a persisted monotone integer).

    The comparison never consults Python object identity, ``id()``, set
    iteration order or hash values, so ``PYTHONHASHSEED`` cannot change it.

A literal word whose text is exactly ``<*>`` is stored with kind ``literal``
and value ``<*>`` -- it is never reinterpreted as a wildcard.  Rendering uses
``LIT(<*>)`` for such a literal, while a learned wildcard renders as ``<*>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from .masks import WILDCARD_RENDER

LITERAL = "literal"
MASK = "mask"
WILDCARD = "wildcard"

ITEM_KINDS = (LITERAL, MASK, WILDCARD)


@dataclass(frozen=True)
class Item:
    kind: str
    value: str  # literal text, mask kind name, or empty for a wildcard

    def __post_init__(self):
        if self.kind not in ITEM_KINDS:
            raise ValueError(f"unknown item kind {self.kind!r}")
        if self.kind == WILDCARD:
            if self.value != "":
                raise ValueError("wildcard items carry no value")
        elif not self.value:
            raise ValueError(f"{self.kind} item needs a non-empty value")

    def render(self) -> str:
        if self.kind == LITERAL:
            if self.value == WILDCARD_RENDER:
                return f"LIT({self.value})"
            return self.value
        if self.kind == MASK:
            return f"<{self.value}>"
        return WILDCARD_RENDER

    @classmethod
    def from_row(cls, kind: str, value: str) -> "Item":
        return cls(kind, "" if kind == WILDCARD else value)


def render_items(items: tuple[Item, ...]) -> str:
    return " ".join(it.render() for it in items)

