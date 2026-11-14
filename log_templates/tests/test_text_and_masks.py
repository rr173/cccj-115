"""Tokenizer and mask-rule unit tests (independent of the index/matcher)."""

from __future__ import annotations

import pytest

from logtempl import masks
from logtempl.textproc import Tokenised, count_words

ALL = masks.validate_enabled(None)


# ---------------------------------------------------------------------------
# tokenisation / exact reconstruction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("message", [
    "",
    " ",
    "\t",
    "\n",
    "   \t \n ",
    "a",
    "  a",
    "a  ",
    " a ",
    "a b",
    "a\t\tb",
    "a \t b",
    "\ta\tb\t",
    "one  two\tthree\nfour",
    "host=10.0.0.1   port=8080",
    "x y",           # non-breaking space is a gap
    "　a　b　",  # ideographic space
    "  <*>  ",
])
def test_reconstruction_is_char_exact(message):
    tok = Tokenised.from_message(message)
    assert tok.reassemble() == message
    assert count_words(message) == len(tok.words)
    assert len(tok.gaps) == len(tok.words) + 1
    for gap in tok.gaps:
        assert all(not word for word in [])  # no-op, keeps intent readable
    # Gaps hold whitespace only; words hold no whitespace.
    for gap in tok.gaps:
        assert all(ch.isspace() for ch in gap)
    for word in tok.words:
        assert word and not any(ch.isspace() for ch in word)


def test_whitespace_runs_are_not_collapsed():
    tok = Tokenised.from_message("a \t\t b")
    assert tok.words == ("a", "b")
    assert tok.gaps == ("", " \t\t ", "")


def test_leading_and_trailing_whitespace_kept():
    tok = Tokenised.from_message("  x  ")
    assert tok.gaps == ("  ", "  ")


def test_empty_message_has_no_words():
    tok = Tokenised.from_message("")
    assert tok.words == ()
    assert tok.gaps == ("",)


# ---------------------------------------------------------------------------
# mask rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("word,kind", [
    ("10.0.0.1", masks.IPV4),
    ("0.0.0.0", masks.IPV4),
    ("255.255.255.255", masks.IPV4),
    ("192.168.1.254", masks.IPV4),
    ("550e8400-e29b-41d4-a716-446655440000", masks.UUID),
    ("550E8400-E29B-41D4-A716-446655440000", masks.UUID),
    ("0", masks.INT),
    ("42", masks.INT),
    ("-7", masks.INT),
    ("+7", masks.INT),
    ("007", masks.INT),
])
def test_whole_word_masks(word, kind):
    assert masks.classify(word, ALL) == kind


@pytest.mark.parametrize("word", [
    "256.0.0.1",            # octet out of range
    "1.2.3.999",
    "1.2.3",                # too few octets
    "1.2.3.4.5",            # too many octets
    "01.2.3.4",             # non-canonical zero padding
    "1.2.3.4.",             # trailing dot
    ".1.2.3.4",
    "10.0.0.1;",            # punctuation attached
    "x10.0.0.1",
    "10.0.0.1/24",
    "{550e8400-e29b-41d4-a716-446655440000}",  # not the standard form
    "550e8400e29b41d4a716446655440000",        # hyphens missing
    "550e8400-e29b-41d4-a716-44665544000g",    # non-hex char
    "1_000",
    "1.0",
    "0x10",
    "1e3",
    "+",
    "-",
    "١٢",                    # non-ASCII digits stay literal
    "<*>",
    "",
])
def test_non_masks_stay_literal(word):
    assert masks.classify(word, ALL) is None


def test_mask_rule_catalogue_is_configurable():
    only_int = masks.validate_enabled(["INT"])
    assert masks.classify("10.0.0.1", only_int) is None
    assert masks.classify("42", only_int) == masks.INT
    only_ip = masks.validate_enabled("IPV4")
    assert masks.classify("42", only_ip) is None
    none_enabled = masks.validate_enabled([])
    assert masks.classify("42", none_enabled) is None


def test_unknown_rule_is_rejected():
    with pytest.raises(ValueError):
        masks.validate_enabled(["IPV6"])


def test_rule_order_is_fixed_and_stable():
    # The evaluation order is a documented tuple, not a set iteration.
    assert masks.MASK_KINDS == (masks.IPV4, masks.UUID, masks.INT)
    enabled = masks.validate_enabled(["INT", "UUID", "IPV4"])
    assert masks.enabled_text(enabled) == "IPV4,UUID,INT"
