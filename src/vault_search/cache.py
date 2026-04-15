"""In-memory tiered search cache (Tier 0 / Tier 1).

ByteRover のプログレッシブ検索を参考にした 2 段キャッシュ:

- Tier 0: ``(query, filters)`` ハッシュによる完全一致 (~0ms)
- Tier 1: Jaccard 類似度 ≥ fuzzy_threshold のファジーヒット (~1ms)

FTS5 検索 (Tier 2) を実行する ``VaultIndex`` が、結果を ``put`` でキャッシュし、
次回以降の ``get`` で Tier 0/1 を試みる。書き込み側は ``invalidate`` で全クリア。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CacheEntry:
    result: list[dict[str, Any]]
    tokens: frozenset[str]
    created_at: float = field(default_factory=time.monotonic)


class TieredCache:
    """Tier 0 (exact) + Tier 1 (fuzzy) キャッシュ."""

    def __init__(self, max_size: int = 256, ttl: float = 300.0, fuzzy_threshold: float = 0.8):
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl
        self._fuzzy_threshold = fuzzy_threshold
        self._lock = threading.Lock()

    def _tokenize(self, query: str) -> frozenset[str]:
        """クエリをトークン集合に変換."""
        return frozenset(query.lower().split())

    def _cache_key(self, query: str, filters: dict[str, Any] | None) -> str:
        raw = query + "|" + json.dumps(filters or {}, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(raw.encode()).hexdigest()

    def _jaccard(self, a: frozenset[str], b: frozenset[str]) -> float:
        if not a and not b:
            return 1.0
        union = a | b
        if not union:
            return 0.0
        return len(a & b) / len(union)

    def get(
        self, query: str, filters: dict[str, Any] | None = None
    ) -> tuple[int, list[dict[str, Any]] | None]:
        """キャッシュ検索。返り値: (tier, results). tier=-1 はミス."""
        now = time.monotonic()
        key = self._cache_key(query, filters)

        with self._lock:
            # Tier 0: 完全一致
            if key in self._store:
                entry = self._store[key]
                if now - entry.created_at < self._ttl:
                    self._store.move_to_end(key)
                    return (0, entry.result)
                else:
                    del self._store[key]

            # Tier 1: ファジー検索（フィルタなしの場合のみ）
            if not filters:
                query_tokens = self._tokenize(query)
                best_score = 0.0
                best_entry: CacheEntry | None = None

                for _k, entry in self._store.items():
                    if now - entry.created_at >= self._ttl:
                        continue
                    score = self._jaccard(query_tokens, entry.tokens)
                    if score > best_score:
                        best_score = score
                        best_entry = entry

                if best_score >= self._fuzzy_threshold and best_entry is not None:
                    return (1, best_entry.result)

        return (-1, None)

    def put(self, query: str, filters: dict[str, Any] | None, result: list[dict[str, Any]]) -> None:
        key = self._cache_key(query, filters)
        tokens = self._tokenize(query)

        with self._lock:
            self._store[key] = CacheEntry(result=result, tokens=tokens)
            self._store.move_to_end(key)

            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def invalidate(self) -> None:
        """全キャッシュクリア（インデックス更新時）."""
        with self._lock:
            self._store.clear()
