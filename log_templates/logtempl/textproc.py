"""Tokenisation of a log message into words and whitespace gaps.

Contract
--------

A message is split into *contiguous non-whitespace pieces* (``LEX`` =
maximal runs of characters whose Unicode category is not ``Z*`` and which
are not in ASCII whitespace).  Everything between two pieces -- and any
leading/trailing run -- is one *gap* recorded verbatim.

The split keeps every character, so for a message of ``n`` words::

    message == gap[0] + word[0] + gap[1] + word[1] + ... + gap[n]

with ``len(gaps) == len(words) + 1``.  Concatenating the gaps and words
back in position rebuilds the original byte-for-character message.  No
normalisation of any kind (no strip, no tab expansion, no collapsing of
runs) happens here or anywhere downstream.

``len(words) == 0`` exactly when the message is empty or consists solely
of whitespace (including a lone ``\\n`` read off the wire).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# ASCII whitespace is covered by str.isspace(), but the explicit table keeps
# the intent visible.  Non-ASCII whitespace is recognised through the Unicode
# separator categories and str.isspace(), which also covers tab/newline/vtab.
_ASCII_WS = frozenset(" \t\n\r\v\f")


def is_whitespace(ch: str) -> bool:
    """True for any code point treated as a separator (never part of a word)."""
    if ch in _ASCII_WS:
        return True
    if ch.isspace():
        return True
    return unicodedata.category(ch).startswith("Z")


def is_word_char(ch: str) -> bool:
    return not is_whitespace(ch)


def count_words(message: str) -> int:
    """Number of contiguous non-whitespace pieces; a fast exact count."""
    n = 0
    inside = False
    for ch in message:
        if is_word_char(ch):
            if not inside:
                n += 1
                inside = True
        else:
            inside = False
    return n


@dataclass(frozen=True)
class Tokenised:
    """A message split into words and preserved gaps.

    ``words`` are non-empty and contain no whitespace; ``gaps`` has exactly
    one more element than ``words``.  ``reassemble`` is the only supported
    way to turn the pair back into text and it is a pure inverse.
    """

    words: tuple[str, ...]
    gaps: tuple[str, ...]

    @classmethod
    def from_message(cls, message: str) -> "Tokenised":
        words: list[str] = []
        gaps: list[str] = []
        start = 0
        i = 0
        n = len(message)
        while i < n:
            if is_word_char(message[i]):
                # begin a word: the preceding gap runs [start, i)
                j = i + 1
                while j < n and is_word_char(message[j]):
                    j += 1
                gaps.append(message[start:i])
                words.append(message[i:j])
                start = j
                i = j
            else:
                i += 1
        gaps.append(message[start:n])
        return cls(tuple(words), tuple(gaps))

    def reassemble(self) -> str:
        if len(self.gaps) != len(self.words) + 1:
            raise ValueError("gap/word arity invariant violated")
        parts: list[str] = [self.gaps[0]]
        for k, word in enumerate(self.words):
            parts.append(word)
            parts.append(self.gaps[k + 1])
        return "".join(parts)

    @property
    def word_count(self) -> int:
        return len(self.words)
