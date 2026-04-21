"""In-memory tiered search cache (Tier 0 / Tier 1).

ByteRover のプログレッシブ検索を参考にした 2 段キャッシュ:

- Tier 0: ``(query, filters)`` ハッシュによる完全一致 (~0ms)
- Tier 1: Jaccard 類似度 ≥ fuzzy_threshold のファジーヒット (~1ms)。
  ``filters`` (tags / folder / metadata_filter) が付与されたクエリは
  スキップし、Tier 0 完全一致のみを適用する。

FTS5 検索 (Tier 2) を実行する ``VaultIndex`` が、結果を ``put`` でキャッシュし、
次回以降の ``get`` で Tier 0/1 を試みる。書き込み側は ``invalidate`` で無効化
するが、``invalidate(changed_paths)`` に変更 note の rel-path 集合を渡すと
その path を結果に含む entry のみを drop する (Issue #31)。``changed_paths``
を省略または ``None`` にした場合は従来通り全 entry を clear する。
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
    total: int
    tokens: frozenset[str]
    # Issue #31: 結果 note の rel-path 集合。``invalidate(changed_paths)`` で
    # path 逆引きによる granular drop に使う。``put`` が result から抽出する。
    paths: frozenset[str] = frozenset()
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
    ) -> tuple[int, CacheEntry | None]:
        """キャッシュ検索。返り値: (tier, entry). tier=-1 はミス。

        Tier 1 (fuzzy) は Jaccard 類似度 >= fuzzy_threshold のとき適用。
        境界値 (= threshold) はヒット扱い。フィルタ付きクエリは Tier 1 をスキップ。

        ヒット時に ``CacheEntry`` 丸ごとを返すのは、``entry.total`` が
        ``len(entry.result)`` より大きいケース (結果が内部 cap で truncate
        された over-cap エントリで accurate な総件数が別途取得された状態) を
        呼び出し側に伝えるため (#17)。under-cap エントリでは
        ``entry.total == len(entry.result)`` が成立する (#166)。
        """
        now = time.monotonic()
        key = self._cache_key(query, filters)

        with self._lock:
            # Tier 0: 完全一致
            if key in self._store:
                entry = self._store[key]
                if now - entry.created_at < self._ttl:
                    self._store.move_to_end(key)
                    return (0, entry)
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
                    return (1, best_entry)

        return (-1, None)

    def put(
        self,
        query: str,
        filters: dict[str, Any] | None,
        result: list[dict[str, Any]],
        *,
        total: int,
    ) -> None:
        """(query, filters) に対する結果を total 付きで格納する.

        ``total`` は呼び出し元が確定した accurate な件数。``VaultIndex.search``
        では under-cap で ``len(result)`` を、over-cap で COUNT(*) クエリの
        値をそれぞれ渡す (#166)。いずれも近似ではなく、``len(result)`` が内部
        cap (``_MAX_RESULTS``) で truncate された場合でも ``total`` は実件数を
        保持するため、ページング終端判定は ``entry.total`` を見ること (#17)。

        **Precondition (#31)**: ``result`` の各要素は rel-path を ``"path"`` キー
        で含むこと。含まれない要素は ``entry.paths`` から silent に抜け、
        ``invalidate(changed_paths)`` での granular drop が効かなくなる
        (そのエントリはいかなる changed_paths でも drop されず TTL まで残る)。
        ``VaultIndex.search`` / ``recent_notes`` は常に ``path`` を含めて呼ぶ。
        """
        key = self._cache_key(query, filters)
        tokens = self._tokenize(query)
        paths = frozenset(r["path"] for r in result if "path" in r)

        with self._lock:
            self._store[key] = CacheEntry(result=result, total=total, tokens=tokens, paths=paths)
            self._store.move_to_end(key)

            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def invalidate(self, changed_paths: set[str] | frozenset[str] | None = None) -> None:
        """キャッシュ無効化 (Issue #31).

        ``changed_paths`` に rel-path 集合を渡すと、その path を結果に含む
        entry のみを drop する (path 逆引き)。``None`` または省略時は従来通り
        全 entry を clear する (full rebuild 経路用)。

        Note: この granular invalidation は「編集した note が既存 cache entry の
        結果 path に含まれている」ケースのみ正確。新規 note が既存 query に
        新たにマッチするケース (cache entry の paths に含まれない) は検知できず、
        TTL (既定 300s) が切れるまで silent に stale のままとなる。編集の大半は
        local な文字修正で既存マッチの変動で足りる前提で、hit rate とのトレード
        オフとして受容する設計 (`.claude/rules/fastmcp-gotchas.md` 相当の
        index-time trust 境界と同じく、新規マッチ検知は build_index の
        full rebuild 経路に委ねる)。Tier 1 fuzzy hit も同じ限界を継承する:
        別クエリの cache entry を fuzzy で流用する場合、その entry の ``paths``
        に含まれない新規マッチは返されない。
        """
        with self._lock:
            if changed_paths is None:
                self._store.clear()
                return
            if not changed_paths:
                return
            stale_keys = [k for k, entry in self._store.items() if entry.paths & changed_paths]
            for k in stale_keys:
                del self._store[k]
