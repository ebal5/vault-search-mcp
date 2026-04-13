"""SQLite + FTS5 インデクサー、3段キャッシュ、ファイル監視.

ByteRover の 5段階プログレッシブ検索 (Tier 0-2) を参考にした設計:
  Tier 0: 完全キャッシュヒット (~0ms)
  Tier 1: ファジーキャッシュ — Jaccard 類似度 ≥ 0.8 (~1ms)
  Tier 2: FTS5 全文検索 — trigram トークナイザ (~10-100ms)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .parser import ParsedNote, parse_note

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    result: list[dict[str, Any]]
    tokens: frozenset[str]
    created_at: float = field(default_factory=time.monotonic)


class TieredCache:
    """Tier 0 (exact) + Tier 1 (fuzzy) キャッシュ."""

    def __init__(
        self, max_size: int = 256, ttl: float = 300.0, fuzzy_threshold: float = 0.8
    ):
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl
        self._fuzzy_threshold = fuzzy_threshold
        self._lock = threading.Lock()

    def _tokenize(self, query: str) -> frozenset[str]:
        """クエリをトークン集合に変換."""
        return frozenset(query.lower().split())

    def _cache_key(self, query: str, filters: dict[str, Any] | None) -> str:
        raw = (
            query + "|" + json.dumps(filters or {}, sort_keys=True, ensure_ascii=False)
        )
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

                for k, entry in self._store.items():
                    if now - entry.created_at >= self._ttl:
                        continue
                    score = self._jaccard(query_tokens, entry.tokens)
                    if score > best_score:
                        best_score = score
                        best_entry = entry

                if best_score >= self._fuzzy_threshold and best_entry is not None:
                    return (1, best_entry.result)

        return (-1, None)

    def put(
        self, query: str, filters: dict[str, Any] | None, result: list[dict[str, Any]]
    ) -> None:
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


# ---------------------------------------------------------------------------
# VaultIndex — SQLite + FTS5
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    aliases TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT '',
    modified_at TEXT NOT NULL DEFAULT '',
    file_mtime REAL NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',
    frontmatter TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder);
CREATE INDEX IF NOT EXISTS idx_notes_mtime ON notes(file_mtime DESC);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title,
    content,
    tags,
    aliases,
    content='notes',
    content_rowid='id',
    tokenize='trigram'
);

-- FTS 同期トリガー
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, content, tags, aliases)
    VALUES (new.id, new.title, new.content, new.tags, new.aliases);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags, aliases)
    VALUES ('delete', old.id, old.title, old.content, old.tags, old.aliases);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags, aliases)
    VALUES ('delete', old.id, old.title, old.content, old.tags, old.aliases);
    INSERT INTO notes_fts(rowid, title, content, tags, aliases)
    VALUES (new.id, new.title, new.content, new.tags, new.aliases);
END;
"""

# メタテーブル（最終スキャン時刻等）
_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class VaultIndex:
    """SQLite + FTS5 によるインデックスと検索エンジン."""

    def __init__(self, vault_root: str | Path, db_path: str | Path | None = None):
        self.vault_root = Path(vault_root).resolve()
        if db_path is None:
            db_path = self.vault_root / ".vault-search.db"
        self.db_path = Path(db_path)

        self._cache = TieredCache()
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.executescript(_FTS_SCHEMA)
            conn.executescript(_META_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8MB
        return conn

    # ------------------------------------------------------------------
    # File iteration (safe)
    # ------------------------------------------------------------------

    # 除外するフォルダ名（先頭一致）
    _EXCLUDED_PREFIXES = (".", "_")

    def _iter_markdown_files(self) -> list[Path]:
        """Vault 内の .md ファイルを安全にイテレーション.

        隠しフォルダ・システムフォルダをスキップし、I/O エラーを吸収する。
        """
        results: list[Path] = []

        def _walk(directory: Path) -> None:
            try:
                entries = list(directory.iterdir())
            except OSError:
                return

            for entry in entries:
                name = entry.name
                # 隠しフォルダ / _ プレフィックスをスキップ
                if any(name.startswith(p) for p in self._EXCLUDED_PREFIXES):
                    continue

                try:
                    if entry.is_dir():
                        _walk(entry)
                    elif entry.is_file() and name.endswith(".md"):
                        results.append(entry)
                except OSError:
                    continue

        _walk(self.vault_root)
        return results

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def build_index(self, *, force: bool = False) -> dict[str, int]:
        """Vault 全体をスキャンしてインデックスを構築.

        force=True で全件リビルド。False なら mtime ベースの差分更新。
        """
        stats = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}
        conn = self._connect()

        try:
            # 現在のファイル一覧
            current_files: dict[str, float] = {}
            for md in self._iter_markdown_files():
                rel = str(md.relative_to(self.vault_root)).replace("\\", "/")
                try:
                    current_files[rel] = md.stat().st_mtime
                except OSError:
                    continue

            # DB 上の既存エントリ
            existing: dict[str, float] = {}
            if not force:
                for row in conn.execute("SELECT path, file_mtime FROM notes"):
                    existing[row["path"]] = row["file_mtime"]

            # 削除されたファイル
            deleted_paths = set(existing.keys()) - set(current_files.keys())
            if deleted_paths:
                conn.executemany(
                    "DELETE FROM notes WHERE path = ?",
                    [(p,) for p in deleted_paths],
                )
                stats["deleted"] = len(deleted_paths)

            # 追加・更新
            for rel_path, mtime in current_files.items():
                if not force and rel_path in existing:
                    if existing[rel_path] >= mtime:
                        stats["skipped"] += 1
                        continue

                full_path = self.vault_root / rel_path
                note = parse_note(full_path, self.vault_root)
                if note is None:
                    stats["errors"] += 1
                    continue

                self._upsert_note(conn, note, mtime)

                if rel_path in existing:
                    stats["updated"] += 1
                else:
                    stats["added"] += 1

            conn.commit()

            # キャッシュ無効化
            self._cache.invalidate()

            logger.info(
                "Index built: added=%d updated=%d deleted=%d skipped=%d errors=%d",
                stats["added"],
                stats["updated"],
                stats["deleted"],
                stats["skipped"],
                stats["errors"],
            )
        finally:
            conn.close()

        return stats

    def _upsert_note(
        self, conn: sqlite3.Connection, note: ParsedNote, mtime: float
    ) -> None:
        conn.execute(
            """INSERT INTO notes (path, title, folder, tags, aliases, created_at, modified_at, file_mtime, content, frontmatter)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   title=excluded.title, folder=excluded.folder, tags=excluded.tags,
                   aliases=excluded.aliases, created_at=excluded.created_at,
                   modified_at=excluded.modified_at, file_mtime=excluded.file_mtime,
                   content=excluded.content, frontmatter=excluded.frontmatter""",
            (
                note.path,
                note.title,
                note.folder,
                note.tags_json,
                json.dumps(note.aliases, ensure_ascii=False),
                note.created_at,
                note.modified_at,
                mtime,
                note.content,
                note.frontmatter_json,
            ),
        )

    def update_single(self, rel_path: str) -> bool:
        """単一ファイルのインデックスを更新."""
        full_path = (self.vault_root / rel_path).resolve()
        try:
            full_path.relative_to(self.vault_root)
        except ValueError:
            logger.warning("Path traversal attempt blocked: %s", rel_path)
            return False
        conn = self._connect()
        try:
            if not full_path.exists():
                conn.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
                conn.commit()
                self._cache.invalidate()
                return True

            note = parse_note(full_path, self.vault_root)
            if note is None:
                return False

            mtime = full_path.stat().st_mtime
            self._upsert_note(conn, note, mtime)
            conn.commit()
            self._cache.invalidate()
            return True
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Search — 3段パイプライン
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        folder: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Tier 0-2 のプログレッシブ検索."""
        filters = {"tags": tags, "folder": folder} if (tags or folder) else None

        # Tier 0-1: キャッシュ
        tier, cached = self._cache.get(query, filters)
        if cached is not None:
            sliced = cached[offset : offset + limit]
            return {
                "tier": tier,
                "total": len(cached),
                "results": sliced,
            }

        # Tier 2: FTS5 — キャッシュ用に上限付きで取得
        _MAX_RESULTS = 500
        results = self._fts5_search(query, tags=tags, folder=folder, limit=_MAX_RESULTS)
        self._cache.put(query, filters, results)

        sliced = results[offset : offset + limit]
        return {
            "tier": 2,
            "total": len(results),
            "results": sliced,
        }

    def _fts5_search(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        folder: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """FTS5 trigram 検索 + メタデータフィルタ."""
        conn = self._connect()
        try:
            terms = query.strip().split()
            if not terms:
                return []

            fts_terms = [t for t in terms if len(t) >= 3]
            short_terms = [t for t in terms if len(t) < 3]

            if fts_terms:
                fts_query = " AND ".join(f'"{t}"' for t in fts_terms)
                sql_parts: list[str] = [
                    "SELECT n.path, n.title, n.folder, n.tags, n.created_at, n.modified_at,",
                    "       snippet(notes_fts, 1, '>>>', '<<<', '...', 64) AS snippet,",
                    "       rank",
                    "FROM notes_fts f",
                    "JOIN notes n ON n.id = f.rowid",
                    "WHERE notes_fts MATCH ?",
                ]
                params: list[Any] = [fts_query]
            else:
                # 全語が3文字未満 — LIKE フォールバック
                sql_parts = [
                    "SELECT n.path, n.title, n.folder, n.tags, n.created_at, n.modified_at,",
                    "       '' AS snippet,",
                    "       0 AS rank",
                    "FROM notes n",
                    "WHERE 1=1",
                ]
                params = []

            # 短い語は LIKE で補完
            for term in short_terms:
                like_param = "%" + term + "%"
                sql_parts.append("AND (n.title LIKE ? OR n.content LIKE ?)")
                params.extend([like_param, like_param])

            # メタデータフィルタ
            if folder:
                folder = folder.replace("\\", "/")
                escaped = folder.replace("%", "\\%").replace("_", "\\_")
                sql_parts.append("AND n.folder LIKE ? ESCAPE '\\'")
                params.append(escaped + "%")

            if tags:
                for tag in tags:
                    sql_parts.append("AND n.tags LIKE ?")
                    params.append(f'%"{tag}"%')

            if fts_terms:
                sql_parts.append("ORDER BY rank")
            else:
                sql_parts.append("ORDER BY n.file_mtime DESC")
            sql_parts.append("LIMIT ?")
            params.append(limit)

            sql = "\n".join(sql_parts)
            rows = conn.execute(sql, params).fetchall()

            return [
                {
                    "path": r["path"],
                    "title": r["title"],
                    "folder": r["folder"],
                    "tags": json.loads(r["tags"]),
                    "created_at": r["created_at"],
                    "modified_at": r["modified_at"],
                    "snippet": r["snippet"],
                    "score": r["rank"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Structured queries (non-FTS)
    # ------------------------------------------------------------------

    def get_note(self, path: str) -> dict[str, Any] | None:
        """指定パスのノート全文を取得."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT path, title, folder, tags, aliases, created_at, modified_at, content, frontmatter FROM notes WHERE path = ?",
                (path,),
            ).fetchone()
            if row is None:
                return None
            return {
                "path": row["path"],
                "title": row["title"],
                "folder": row["folder"],
                "tags": json.loads(row["tags"]),
                "aliases": json.loads(row["aliases"]),
                "created_at": row["created_at"],
                "modified_at": row["modified_at"],
                "content": row["content"],
                "frontmatter": json.loads(row["frontmatter"]),
            }
        finally:
            conn.close()

    def recent_notes(
        self, limit: int = 20, folder: str | None = None
    ) -> list[dict[str, Any]]:
        """最近更新されたノート."""
        conn = self._connect()
        try:
            if folder:
                rows = conn.execute(
                    "SELECT path, title, folder, tags, created_at, modified_at FROM notes WHERE folder LIKE ? ORDER BY file_mtime DESC LIMIT ?",
                    (f"{folder}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT path, title, folder, tags, created_at, modified_at FROM notes ORDER BY file_mtime DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {
                    "path": r["path"],
                    "title": r["title"],
                    "folder": r["folder"],
                    "tags": json.loads(r["tags"]),
                    "created_at": r["created_at"],
                    "modified_at": r["modified_at"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def list_tags(self) -> list[dict[str, Any]]:
        """全タグと出現回数を返す."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT tags FROM notes").fetchall()
            tag_count: dict[str, int] = {}
            for row in rows:
                for tag in json.loads(row["tags"]):
                    tag_count[tag] = tag_count.get(tag, 0) + 1
            return sorted(
                [{"tag": t, "count": c} for t, c in tag_count.items()],
                key=lambda x: x["count"],
                reverse=True,
            )
        finally:
            conn.close()

    def list_folders(self) -> list[dict[str, Any]]:
        """フォルダと含まれるノート数を返す."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT folder, COUNT(*) as count FROM notes GROUP BY folder ORDER BY folder"
            ).fetchall()
            return [
                {"folder": r["folder"] or "(root)", "count": r["count"]} for r in rows
            ]
        finally:
            conn.close()

    def stats(self) -> dict[str, Any]:
        """インデックスの統計情報."""
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            return {
                "total_notes": total,
                "db_size_bytes": db_size,
                "db_size_mb": round(db_size / (1024 * 1024), 2),
                "vault_root": str(self.vault_root),
            }
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# File Watcher
# ---------------------------------------------------------------------------


class VaultWatcher:
    """watchdog ベースのファイル監視.

    watchdog が無い環境では polling フォールバック。
    """

    def __init__(self, index: VaultIndex, debounce_sec: float = 2.0):
        self._index = index
        self._debounce_sec = debounce_sec
        self._observer: Any = None
        self._pending: dict[str, float] = {}
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """監視開始。watchdog が利用可能なら True."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileSystemEvent

            watcher = self

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event: FileSystemEvent) -> None:
                    if event.is_directory:
                        return
                    src = event.src_path
                    if not src.endswith(".md"):
                        return
                    # 隠しフォルダ除外
                    try:
                        rel = str(
                            Path(src).relative_to(watcher._index.vault_root)
                        ).replace("\\", "/")
                    except ValueError:
                        return
                    if any(
                        p.startswith(".") or p.startswith("_") for p in Path(rel).parts
                    ):
                        return
                    watcher._schedule_update(rel)

            self._observer = Observer()
            self._observer.schedule(
                Handler(), str(self._index.vault_root), recursive=True
            )
            self._observer.daemon = True
            self._observer.start()
            logger.info("File watcher started: %s", self._index.vault_root)
            return True

        except ImportError:
            logger.warning("watchdog not installed — file watching disabled")
            return False

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _schedule_update(self, rel_path: str) -> None:
        """デバウンス付きインデックス更新スケジューリング."""
        with self._lock:
            self._pending[rel_path] = time.monotonic()

            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_sec, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths = list(self._pending.keys())
            self._pending.clear()

        for rel_path in paths:
            try:
                self._index.update_single(rel_path)
                logger.debug("Updated index: %s", rel_path)
            except Exception:
                logger.exception("Failed to update index for %s", rel_path)
