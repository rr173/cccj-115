"""Bounded caches: eviction is transparent, restart equivalence, determinism."""

from __future__ import annotations

import random

import reference as ref

from conftest import dump_library, ingest, write_stream, run_cli
from logtempl.cache import CappedCache
from logtempl.library import Library


# ---------------------------------------------------------------------------
# cache unit behaviour
# ---------------------------------------------------------------------------
def test_cache_eviction_order_and_bounds():
    cache = CappedCache(3)
    loads = []
    for key in range(10):
        cache.get(key, lambda k: loads.append(k) or k * 10)
    assert len(cache) == 3
    assert cache.limit == 3
    assert cache.evictions == 7
    assert sorted(cache.keys()) == [7, 8, 9]


def test_cache_hit_refreshes_recency():
    cache = CappedCache(2)
    cache.get("a", lambda k: 1)
    cache.get("b", lambda k: 2)
    cache.get("a", lambda k: 99)  # hit, refreshes 'a'
    cache.get("c", lambda k: 3)   # evicts 'b'
    assert sorted(cache.keys()) == ["a", "c"]


def test_zero_limit_cache_stores_nothing():
    cache = CappedCache(0)
    value = cache.get("x", lambda k: 5)
    assert value == 5
    assert len(cache) == 0
    assert cache.stats()["size"] == 0


def test_cache_evicted_value_is_reloaded_from_source():
    backing = {i: i * i for i in range(5)}
    cache = CappedCache(1)
    for _ in range(3):
        for i in range(5):
            assert cache.get(i, backing.__getitem__) == i * i
    assert cache.evictions > 0


# ---------------------------------------------------------------------------
# tiny caches over many templates
# ---------------------------------------------------------------------------
def _many_messages(n):
    # Shared depth-2 prefix, distinct tails: every lookup reaches the same
    # leaf node and scores every template there, so the item cache is used
    # for each candidate and a 2-entry cache must evict constantly.
    return [
        f"alpha beta worker{n:03d} job{n:03d} status ok level {n % 7}"
        for n in range(n)
    ]


def test_tiny_cache_continues_correctly_after_restart(db_path):
    # With threshold 1, every message shares the first four words of some
    # template, but an exact match requires all nine; many candidates are
    # scored per message, so a 2-entry item cache evicts on every batch.
    messages = _many_messages(120)
    with Library.create(db_path, threshold="1", depth=2,
                        template_cache=2, node_cache=2) as lib:
        ingest(lib, messages[:60])
        assert lib.cache_stats()["templates"]["evictions"] > 0
        assert lib.cache_stats()["nodes"]["evictions"] > 0
        lib.verify_integrity()
    with Library.open(db_path, template_cache=1, node_cache=1) as lib:
        ingest(lib, messages[60:], start=60)
        lib.verify_integrity()

    # Same messages, one process, huge caches
    other = db_path.replace(".db", "-big.db")
    with Library.create(other, threshold="1", depth=2,
                        template_cache=100000, node_cache=100000) as lib:
        ingest(lib, messages)
        lib.verify_integrity()
        big = dump_library(lib, include_tx=False)

    with Library.open(db_path, template_cache=1, node_cache=1) as lib:
        small = dump_library(lib, include_tx=False)
    assert small == big

    # And the same input replayed message by message is idempotent.
    with Library.open(other) as lib:
        outcome = lib.ingest_batch([
            __import__("logtempl.library", fromlist=["Record"]).Record(
                "s", i, m
            )
            for i, m in enumerate(messages)
        ])
        assert outcome.accepted == []
        assert len(outcome.ignored_replays) == len(messages)


def test_cache_limits_are_configuration_not_state(db_path):
    with Library.create(db_path, threshold="0.5", template_cache=64,
                        node_cache=256) as lib:
        ingest(lib, ["a b c"])
    with Library.open(db_path, template_cache=1, node_cache=0) as lib:
        assert lib.config.template_cache == 1
        assert lib.config.node_cache == 0
        assert lib.template_head(1)["render"] == "a b c"
    with Library.open(db_path, template_cache=64, node_cache=256) as lib:
        assert lib.config.template_cache == 64


def test_eviction_does_not_change_ids_or_revisions(db_path):
    messages = _many_messages(40) + _many_messages(10)
    with Library.create(db_path, threshold="1", template_cache=0,
                        node_cache=0) as lib:
        ingest(lib, messages)
        no_cache = dump_library(lib, include_tx=False)
    other = db_path.replace(".db", "-cached.db")
    with Library.create(other, threshold="1", template_cache=1000,
                        node_cache=1000) as lib:
        ingest(lib, messages)
        cached = dump_library(lib, include_tx=False)
    assert no_cache == cached
