"""SQLite + FTS5 インデクサー.

ByteRover の 5段階プログレッシブ検索 (Tier 0-2) を参考にした設計:
  Tier 0: 完全キャッシュヒット (~0ms) — ``cache.TieredCache``
  Tier 1: ファジーキャッシュ — Jaccard 類似度 ≥ 0.8 (~1ms) — 同上
  Tier 2: FTS5 全文検索 — trigram トークナイザ (~10-100ms) — 本モジュール

ファイル監視は ``watcher.VaultWatcher`` が担う。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .cache import TieredCache
from .filter import MetadataCondition, build_sql_fragment, parse_metadata_filter
from .parser import ParsedNote, parse_note

logger = logging.getLogger(__name__)


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


def _folder_filter_clause(folder: str, column: str = "folder") -> tuple[str, list[Any]]:
    """folder プレフィックスフィルタを「folder 自身 OR その配下」に厳密化する.

    `folder == 'Projects'` が `'Projects Hermes'` 等の兄弟を拾わないよう、
    LIKE パターンを ``escaped + '/%'`` にし、等号比較と OR で結合する。

    入力は `\\` → `/` 置換、末尾 `/` の rstrip で正規化する。正規化後が空
    文字列 (e.g. `folder='/'`, `'//'`, `'\\'`) の場合は「フィルタなし」を意味
    する no-op 断片 ``"1=1"`` と空 params を返す。

    Parameters
    ----------
    folder:
        フォルダパス。空文字はフィルタを掛けないので呼び出し側で分岐する前提
        だが、正規化後に空となるケース (スラッシュのみ) は本関数が no-op で吸収する。
    column:
        SQL 上のカラム名。`n.folder` のようにテーブルエイリアス付きも可。

    Returns
    -------
    tuple[str, list[Any]]
        ``(clause, params)``。clause は前置詞を含まない WHERE 断片
        ``"(col = ? OR col LIKE ? ESCAPE '\\')"``、params は ``[folder, folder/%]``。
        正規化後空の場合は ``("1=1", [])``。
    """
    folder = folder.replace("\\", "/").rstrip("/")
    if not folder:
        # `/`, `//`, `\\` 等のスラッシュのみ入力は rstrip 後 '' となり、
        # 呼び出し側の `if folder:` ガードは素通りしている。ここで no-op
        # 断片 ("1=1") を返し「フィルタなし」扱いに揃える (Issue #34)。
        return "1=1", []
    escaped = folder.replace("%", "\\%").replace("_", "\\_")
    clause = f"({column} = ? OR {column} LIKE ? ESCAPE '\\')"
    return clause, [folder, escaped + "/%"]


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
        with self.connection() as conn:
            conn.executescript(_SCHEMA)
            conn.executescript(_FTS_SCHEMA)
            conn.executescript(_META_SCHEMA)
            conn.commit()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """PRAGMA 適用済み SQLite 接続を context-manager で貸与する.

        finally で確実に ``close()`` するボイラープレートを集約する。トランザクション
        管理は呼び出し側で ``conn.commit()`` を明示すること (例外時は自動ロールバック
        相当で単に close される)。
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8MB
        try:
            yield conn
        finally:
            conn.close()

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
        with self.connection() as conn:
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

        return stats

    def _upsert_note(self, conn: sqlite3.Connection, note: ParsedNote, mtime: float) -> None:
        conn.execute(
            """INSERT INTO notes (
                   path, title, folder, tags, aliases,
                   created_at, modified_at, file_mtime, content, frontmatter
               )
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
        rel_path = rel_path.replace("\\", "/")
        full_path = (self.vault_root / rel_path).resolve()
        try:
            full_path.relative_to(self.vault_root)
        except ValueError:
            logger.warning("Path traversal attempt blocked: %s", rel_path)
            return False
        with self.connection() as conn:
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

    # ------------------------------------------------------------------
    # Search — 3段パイプライン
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        folder: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        known_keys: Sequence[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Tier 0-2 のプログレッシブ検索.

        Parameters
        ----------
        query:
            FTS5 trigram 検索に渡すスペース区切りクエリ。空文字も可
            (その場合は ``tags`` / ``folder`` / ``metadata_filter`` の
            いずれかが必要)。
        tags:
            AND 条件でマッチさせるタグのリスト。
        folder:
            フォルダパスフィルタ。指定フォルダ自身 (``n.folder == folder``) と
            その配下 (``n.folder LIKE folder + '/%'``) のみを対象とする。
            同プレフィックス兄弟 (e.g. ``Projects Hermes``) は除外される。
        metadata_filter:
            frontmatter 任意プロパティを対象とする AND フィルタ。
            構文は :func:`vault_search.filter.parse_metadata_filter` を参照。
            不正構造は :class:`ValidationError` を送出する。
        known_keys:
            ``metadata_filter`` のキー検証に使う既知 frontmatter キーのリスト。
            渡された場合は unknown frontmatter key を ``ValidationError`` として
            拒否する (agent UX 向け、Issue #19)。``None`` の場合はキー検証を
            スキップするため後方互換が保たれる。
        limit, offset:
            ページング用。

        Notes
        -----
        ``query`` が空でも ``tags`` / ``folder`` / ``metadata_filter`` の
        いずれかが指定されていれば、DB 全体を対象に構造化フィルタだけで
        絞り込む。全引数が空の場合は空結果を返す。
        """
        # Validate (raises ValidationError on malformed input)
        conditions = parse_metadata_filter(metadata_filter, known_keys=known_keys)

        filters: dict[str, Any] | None = None
        if tags or folder or metadata_filter:
            filters = {
                "tags": tags,
                "folder": folder,
                "metadata_filter": metadata_filter,
            }

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
        results = self._fts5_search(
            query,
            tags=tags,
            folder=folder,
            metadata_conditions=conditions,
            limit=_MAX_RESULTS,
        )
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
        metadata_conditions: list[MetadataCondition] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """FTS5 trigram 検索 + 構造化メタデータフィルタ.

        ``metadata_conditions`` は :func:`build_sql_fragment` 経由で
        SQL WHERE 断片に展開される。クエリが空でも ``tags`` / ``folder`` /
        ``metadata_conditions`` のいずれかがあれば、FTS5 を経由せず
        フィルタ専用パスで DB 全体を走査する。
        """
        with self.connection() as conn:
            terms = query.strip().split()

            fts_terms = [t for t in terms if len(t) >= 3]
            short_terms = [t for t in terms if len(t) < 3]

            has_any_filter = bool(short_terms or tags or folder or metadata_conditions)
            if not terms and not has_any_filter:
                # 空クエリかつフィルタ無し — 従来通り空結果
                return []

            if fts_terms:
                # FTS5 phrase 内の `"` は `""` にダブルして構文エラーを防ぐ
                fts_query = " AND ".join('"' + t.replace('"', '""') + '"' for t in fts_terms)
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
                # 全語が3文字未満 or 空クエリ + フィルタ — LIKE/フィルタ専用パス
                sql_parts = [
                    "SELECT n.path, n.title, n.folder, n.tags, n.created_at, n.modified_at,",
                    "       '' AS snippet,",
                    "       0 AS rank",
                    "FROM notes n",
                    "WHERE 1=1",
                ]
                params = []

            # 短い語は LIKE で補完。'%' '_' '\' はエスケープしてワイルドカード誤ヒットを防ぐ
            for term in short_terms:
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like_param = "%" + escaped + "%"
                sql_parts.append(r"AND (n.title LIKE ? ESCAPE '\' OR n.content LIKE ? ESCAPE '\')")
                params.extend([like_param, like_param])

            # メタデータフィルタ — folder は同プレフィックス兄弟の誤マッチを避ける
            if folder:
                clause, folder_params = _folder_filter_clause(folder, column="n.folder")
                sql_parts.append(f"AND {clause}")
                params.extend(folder_params)

            if tags:
                for tag in tags:
                    sql_parts.append("AND n.tags LIKE ?")
                    params.append(f'%"{tag}"%')

            if metadata_conditions:
                for cond in metadata_conditions:
                    fragment, fragment_params = build_sql_fragment(cond)
                    sql_parts.append(fragment)
                    params.extend(fragment_params)

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

    # ------------------------------------------------------------------
    # Structured queries (non-FTS)
    # ------------------------------------------------------------------

    def get_note(self, path: str) -> dict[str, Any] | None:
        """指定パスのノート全文を取得."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT path, title, folder, tags, aliases, created_at, modified_at, "
                "content, frontmatter FROM notes WHERE path = ?",
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

    def recent_notes(
        self,
        limit: int = 20,
        offset: int = 0,
        folder: str | None = None,
    ) -> list[dict[str, Any]]:
        """最近更新されたノート. offset スキップ後 limit 件を返す."""
        with self.connection() as conn:
            if folder:
                clause, folder_params = _folder_filter_clause(folder, column="folder")
                rows = conn.execute(
                    "SELECT path, title, folder, tags, created_at, modified_at FROM notes "
                    f"WHERE {clause} "
                    "ORDER BY file_mtime DESC LIMIT ? OFFSET ?",
                    (*folder_params, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT path, title, folder, tags, created_at, modified_at FROM notes "
                    "ORDER BY file_mtime DESC LIMIT ? OFFSET ?",
                    (limit, offset),
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

    def list_tags(self) -> list[dict[str, Any]]:
        """全タグと出現回数を返す."""
        with self.connection() as conn:
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

    def list_folders(self) -> list[dict[str, Any]]:
        """フォルダと含まれるノート数を返す."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT folder, COUNT(*) as count FROM notes GROUP BY folder ORDER BY folder"
            ).fetchall()
            return [{"folder": r["folder"], "count": r["count"]} for r in rows]

    def list_frontmatter_keys(self) -> list[str]:
        """Vault 内 frontmatter のトップレベルキーをソート済みで返す."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT key FROM notes, json_each(notes.frontmatter) "
                "WHERE json_valid(notes.frontmatter) ORDER BY key"
            ).fetchall()
            return [r["key"] for r in rows]

    def stats(self) -> dict[str, Any]:
        """インデックスの統計情報."""
        with self.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            return {
                "total_notes": total,
                "db_size_bytes": db_size,
                "db_size_mb": round(db_size / (1024 * 1024), 2),
                "vault_root": str(self.vault_root),
            }
