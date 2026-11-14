"""Bounded in-memory caches whose entries are always recoverable from SQLite.

Every cached value is derived state that also exists as a database row:
template items are in ``template_items`` and index nodes/leaves in
``index_nodes`` / ``index_leaves``.  Evicting an entry therefore cannot alter
behavior; the loader simply re-reads the row.  Caches are insertion-ordered
LRU with a hard entry cap, so memory is bounded regardless of template count.

Hit / miss / eviction counters are exposed for tests that need to prove the
cache is actually bounded and exercised.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Generic, Hashable, Iterator, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class CappedCache(Generic[K, V]):
    def __init__(self, limit: int):
        if limit < 0:
            raise ValueError("cache limit must be >= 0")
        self.limit = limit
        self._data: OrderedDict[K, V] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: K, loader: Callable[[K], V]) -> V:
        """Fetch ``key``, loading on miss.  ``limit == 0`` never stores."""
        if self.limit == 0:
            self.misses += 1
            return loader(key)
        if key in self._data:
            self.hits += 1
            self._data.move_to_end(key)
            return self._data[key]
        self.misses += 1
        value = loader(key)
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.limit:
            self._data.popitem(last=False)
            self.evictions += 1
        return value

    def put(self, key: K, value: V) -> None:
        if self.limit == 0:
            return
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.limit:
            self._data.popitem(last=False)
            self.evictions += 1

    def drop(self, key: K) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def keys(self) -> Iterator[K]:
        return iter(list(self._data))

    def stats(self) -> dict[str, int]:
        return {
            "limit": self.limit,
            "size": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
        }
