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
import re
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .cache import TieredCache
from .filter import (
    MetadataCondition,
    build_folder_filter_clause,
    build_sql_fragment,
    parse_metadata_filter,
)
from .parser import ParsedNote, parse_note
from .schema_meta import FrontmatterKeyInfo
from .validation import normalize_folder

logger = logging.getLogger(__name__)


_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def _infer_value_type(value: Any) -> str:
    """value 1 つから value_type を推論する (None は呼び出し側で除外済み前提)."""
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, str) and value in ("true", "false"):
        return "boolean"
    if isinstance(value, str) and _NUMBER_RE.fullmatch(value):
        return "number"
    return "string"


def _collect_key_info(
    obj: Any,
    prefix: str,
    value_counters: dict[str, Counter[str]],
    type_sets: dict[str, set[str]],
    note_counts: dict[str, int],
) -> None:
    """frontmatter dict を再帰 walk し、各 key の value_counter / type_set / note_count を更新する.

    - 親 dict キー (e.g. 'meta') も含める (value_type='object'、#136 既存契約)
    - ネスト dict は dotted key として葉ノードを別に集計
    - None は note_count から除外
    - 空文字は sample_values (counter) からは除外するが note_count には含める
    - list 要素の dict は走査しない (SQL の $.tags.x パス不成立のため)
    """
    if not isinstance(obj, dict):
        return
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k

        # None は全集計から除外
        if v is None:
            continue

        # note_count は常に +1 (空文字も含む)。
        # note_counts.keys() を「この走査で観測された key 全集合」の正準 source of
        # truth として扱い、呼び出し側はこれを iterate する (Reviewer D6)。
        note_counts[key] = note_counts.get(key, 0) + 1

        # value_type 推論
        vtype = _infer_value_type(v)
        type_sets.setdefault(key, set()).add(vtype)

        # sample_values の追加 — 値を持つ経路のみ Counter エントリを作る。
        # 親 dict (object) は sample を持たないので value_counters に登録しない。
        if isinstance(v, dict):
            # 親キーのサンプルは空。子を再帰 walk する。
            _collect_key_info(v, key, value_counters, type_sets, note_counts)
        elif isinstance(v, list):
            # 配列全体の JSON 文字列表現を sample に入れる (要素別展開はしない)。
            sample_repr = json.dumps(v, ensure_ascii=False)
            value_counters.setdefault(key, Counter())[sample_repr] += 1
        elif isinstance(v, str):
            # 空文字 / whitespace-only は sample から除外 (note_count は +1 済)。
            if v.strip():
                value_counters.setdefault(key, Counter())[v] += 1
        else:
            # _normalize_fm により scalar は str/list/dict/None に収束しているはず。
            # 念のため str 化。
            value_counters.setdefault(key, Counter())[str(v)] += 1


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

    # FTS5 / filter-only 結果をキャッシュに保持する上限 (Issue #17)。
    # ``total`` は別途 COUNT(*) で取得するため ``_MAX_RESULTS`` を超えても
    # accurate な件数がクライアントに届く。``truncated`` フラグで
    # 「cache 側で切り詰められたか」を明示する。
    _MAX_RESULTS = 500

    def __init__(self, vault_root: str | Path, db_path: str | Path | None = None):
        self.vault_root = Path(vault_root).resolve()
        if db_path is None:
            db_path = self.vault_root / ".vault-search.db"
        self.db_path = Path(db_path)

        self._cache = TieredCache()
        self._lock = threading.Lock()
        self._frontmatter_keys_cache: list[FrontmatterKeyInfo] | None = None
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

    def is_indexable_path(self, raw_path: str | Path) -> str | None:
        """Vault 内のインデックス対象パスかを判定し、該当すれば rel path を返す.

        フィルタ条件:

        - ``.md`` 拡張子
        - ``vault_root`` 配下
        - パス構成要素に ``.`` / ``_`` プレフィックスを含まない

        walker (``_iter_markdown_files``) と watcher Handler
        (``VaultEventHandler``) 双方の単一ソースとし、将来の除外ルール拡張で
        drift しないようにする (#76)。
        """
        s = str(raw_path)
        if not s.endswith(".md"):
            return None
        try:
            rel = str(Path(s).relative_to(self.vault_root)).replace("\\", "/")
        except ValueError:
            return None
        if any(p.startswith(self._EXCLUDED_PREFIXES) for p in Path(rel).parts):
            return None
        return rel

    def _iter_markdown_files(self) -> list[Path]:
        """Vault 内の .md ファイルを安全にイテレーション.

        隠しフォルダ・システムフォルダをスキップし、I/O エラーを吸収する。
        leaf 判定は ``is_indexable_path`` に委譲して Handler 側とロジックを共有する。
        """
        results: list[Path] = []

        def _walk(directory: Path) -> None:
            try:
                entries = list(directory.iterdir())
            except OSError:
                return

            for entry in entries:
                name = entry.name
                # 隠しフォルダ / _ プレフィックスをスキップ (traversal 最適化)
                if any(name.startswith(p) for p in self._EXCLUDED_PREFIXES):
                    continue

                try:
                    if entry.is_dir():
                        _walk(entry)
                    elif entry.is_file() and self.is_indexable_path(entry) is not None:
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

        force=False (デフォルト): mtime 差分更新。変更ファイルのみ UPSERT、消失ファイルを DELETE。
        force=True: 全件リビルド。既存 DB レコードを無視して全ファイルを再パース・UPSERT する。
        どちらも vault (.md) は読み取りのみ。更新対象は .vault-search.db のみ。
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

            self._invalidate_caches()

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

    def note_exists(self, rel_path: str) -> bool:
        """``rel_path`` が ``notes`` テーブルに存在するかを返す.

        テストや watcher 経路から index 状態を確認するための公開ヘルパ。
        ``connection()`` を直接開く負担を集約し、private API への依存を防ぐ (#79)。
        """
        rel_path = rel_path.replace("\\", "/")
        with self.connection() as conn:
            row = conn.execute("SELECT 1 FROM notes WHERE path = ?", (rel_path,)).fetchone()
        return row is not None

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
                self._invalidate_caches()
                return True

            note = parse_note(full_path, self.vault_root)
            if note is None:
                return False

            mtime = full_path.stat().st_mtime
            self._upsert_note(conn, note, mtime)
            conn.commit()
            self._invalidate_caches()
            return True

    def _invalidate_caches(self) -> None:
        """書込み経路で tiered cache と frontmatter_keys cache を同時に落とす.

        ``_frontmatter_keys_cache`` の書込みは ``self._lock`` 下で行い、
        ``list_frontmatter_keys()`` の snapshot pattern と対称化する
        (Round 2 E1)。``_cache.invalidate()`` は自身で thread-safe。
        """
        self._cache.invalidate()
        with self._lock:
            self._frontmatter_keys_cache = None

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
            unknown frontmatter key は ``list_frontmatter_keys()`` を内部で
            呼び出して自己検証する (Issue #119)。``UNKNOWN_FRONTMATTER_KEY``
            を持つ ``ValidationError`` として拒否される。
        limit, offset:
            ページング用。

        Notes
        -----
        ``query`` が空でも ``tags`` / ``folder`` / ``metadata_filter`` の
        いずれかが指定されていれば、DB 全体を対象に構造化フィルタだけで
        絞り込む。全引数が空の場合は空結果を返す。
        """
        # folder を canonical 形式に正規化する。先頭 '/' や '\\' 区切りの
        # 入力を吸収し、スラッシュのみの場合は None (フィルタなし) にする。
        folder = normalize_folder(folder) if folder is not None else None

        # Validate (raises ValidationError on malformed input).
        # metadata_filter が指定された場合のみ known_keys を取得する
        # (filter なしでは不要なので呼ばない)。list_frontmatter_keys() は
        # 書込み経路で invalidate される in-memory cache 付き (#118)。
        # value_type='object' の親 dict キーは filter 不可 (SQL 上は dict が返り
        # 文字列比較で常に false → silent 0 件) なので known_keys から除外し、
        # agent が誤って親キーで filter すると UNKNOWN_FRONTMATTER_KEY で通知する。
        # object_keys を別途渡すことで、UNKNOWN エラーメッセージに
        # 「親 dict なので dotted leaf key を使え」の hint を付与する (Round 2 E2)。
        known_keys: list[str] | None = None
        object_keys: list[str] = []
        if metadata_filter:
            _key_infos = self.list_frontmatter_keys()
            known_keys = [info.key for info in _key_infos if info.value_type != "object"]
            object_keys = [info.key for info in _key_infos if info.value_type == "object"]
        conditions = parse_metadata_filter(
            metadata_filter, known_keys=known_keys, object_keys=object_keys
        )

        filters: dict[str, Any] | None = None
        if tags or folder or metadata_filter:
            filters = {
                "tags": tags,
                "folder": folder,
                "metadata_filter": metadata_filter,
            }

        # Tier 0-1: キャッシュ
        tier, entry = self._cache.get(query, filters)
        if entry is not None:
            sliced = entry.result[offset : offset + limit]
            return {
                "tier": tier,
                "total": entry.total,
                "truncated": entry.total > self._MAX_RESULTS,
                "results": sliced,
            }

        # Tier 2: FTS5 — 同一接続で FETCH と COUNT を実行する。
        # 別接続に分けると WAL の snapshot が食い違い、削除競合で
        # `total < len(results)` のような非整合が発生しうる (PR #165 review A1)。
        with self.connection() as conn:
            results = self._fts5_search(
                query,
                tags=tags,
                folder=folder,
                metadata_conditions=conditions,
                limit=self._MAX_RESULTS,
                conn=conn,
            )
            total = self._count_matches(
                query,
                tags=tags,
                folder=folder,
                metadata_conditions=conditions,
                conn=conn,
            )
        self._cache.put(query, filters, results, total=total)

        sliced = results[offset : offset + limit]
        return {
            "tier": 2,
            "total": total,
            "truncated": total > self._MAX_RESULTS,
            "results": sliced,
        }

    def _build_match_clause(
        self,
        query: str,
        *,
        tags: list[str] | None,
        folder: str | None,
        metadata_conditions: list[MetadataCondition] | None,
    ) -> tuple[list[str], list[Any], bool] | None:
        """検索 WHERE 句と params を組み立て、``_fts5_search`` と
        ``_count_matches`` で共有する.

        返り値は ``(from_where_sql_parts, params, is_fts)``。
        空クエリかつフィルタ無し (= 空結果) のケースでは ``None`` を返す。
        ``is_fts`` は FTS5 インデックスを経由するか (= ``ORDER BY rank`` /
        snippet が使える) を示す。
        """
        terms = query.strip().split()
        fts_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]

        has_any_filter = bool(short_terms or tags or folder or metadata_conditions)
        if not terms and not has_any_filter:
            return None

        params: list[Any] = []
        if fts_terms:
            fts_query = " AND ".join('"' + t.replace('"', '""') + '"' for t in fts_terms)
            sql_parts: list[str] = [
                "FROM notes_fts f",
                "JOIN notes n ON n.id = f.rowid",
                "WHERE notes_fts MATCH ?",
            ]
            params.append(fts_query)
        else:
            sql_parts = [
                "FROM notes n",
                "WHERE 1=1",
            ]

        for term in short_terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like_param = "%" + escaped + "%"
            sql_parts.append(r"AND (n.title LIKE ? ESCAPE '\' OR n.content LIKE ? ESCAPE '\')")
            params.extend([like_param, like_param])

        if folder:
            clause, folder_params = build_folder_filter_clause(folder, column="n.folder")
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

        return sql_parts, params, bool(fts_terms)

    def _fts5_search(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        folder: str | None = None,
        metadata_conditions: list[MetadataCondition] | None = None,
        limit: int = 50,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """FTS5 trigram 検索 + 構造化メタデータフィルタ.

        ``metadata_conditions`` は :func:`build_sql_fragment` 経由で
        SQL WHERE 断片に展開される。クエリが空でも ``tags`` / ``folder`` /
        ``metadata_conditions`` のいずれかがあれば、FTS5 を経由せず
        フィルタ専用パスで DB 全体を走査する。

        ``conn`` が与えられたらその接続で実行し、caller が同じ接続で
        COUNT(*) を続発できるようにする (snapshot 整合性のため)。
        ``None`` なら新規接続を開く。
        """
        built = self._build_match_clause(
            query,
            tags=tags,
            folder=folder,
            metadata_conditions=metadata_conditions,
        )
        if built is None:
            return []
        from_where, params, is_fts = built

        if is_fts:
            select = (
                "SELECT n.path, n.title, n.folder, n.tags, n.created_at, n.modified_at,\n"
                "       snippet(notes_fts, 1, '>>>', '<<<', '...', 64) AS snippet,\n"
                "       rank"
            )
            order = "ORDER BY rank"
        else:
            select = (
                "SELECT n.path, n.title, n.folder, n.tags, n.created_at, n.modified_at,\n"
                "       '' AS snippet,\n"
                "       0 AS rank"
            )
            order = "ORDER BY n.file_mtime DESC"

        sql = "\n".join([select, *from_where, order, "LIMIT ?"])
        exec_params = [*params, limit]

        if conn is not None:
            rows = conn.execute(sql, exec_params).fetchall()
        else:
            with self.connection() as c:
                rows = c.execute(sql, exec_params).fetchall()

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

    def _count_matches(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        folder: str | None = None,
        metadata_conditions: list[MetadataCondition] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """``_fts5_search`` と同じ WHERE でマッチ件数を数える (Issue #17).

        ``_MAX_RESULTS`` での truncation を避け、ページング終端をエージェントに
        正しく伝えるために別クエリで発行する。

        ``conn`` を渡せば caller と同一接続 (= 同一 WAL snapshot) で実行される。
        ``_fts5_search`` と pair で呼ぶときは必ず同じ接続を使うこと。
        """
        built = self._build_match_clause(
            query,
            tags=tags,
            folder=folder,
            metadata_conditions=metadata_conditions,
        )
        if built is None:
            return 0
        from_where, params, _is_fts = built

        sql = "\n".join(["SELECT COUNT(*) AS c", *from_where])
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.connection() as c:
                row = c.execute(sql, params).fetchone()
        return int(row["c"]) if row is not None else 0

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
        folder = normalize_folder(folder) if folder is not None else None
        with self.connection() as conn:
            if folder:
                clause, folder_params = build_folder_filter_clause(folder, column="folder")
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

    def list_frontmatter_keys(self) -> list[FrontmatterKeyInfo]:
        """Vault 内 frontmatter のキー別メタ情報をソート済みで返す (Issue #20).

        トップレベルキーに加え、ネスト dict 値は dotted key (``meta.author``)
        としても含まれる (Issue #136)。validate_identifier / SQL が dotted 形式を
        受理するので、known_keys 側も一貫して dotted を公開し
        ``metadata_filter={"meta.author": ...}`` の false positive UNKNOWN を防ぐ。

        各要素は :class:`FrontmatterKeyInfo` で value_type / sample_values /
        note_count を持つ。

        初回呼出時に DB をスキャンしてキャッシュし、以降は書込み経路で
        invalidate されるまでキャッシュを返す (Issue #118 / #10)。

        並行処理対策 (A4 + Round 2 E1):
        - double-checked locking で初回 DB scan の重複実行を防ぐ
        - lock 下で snapshot 参照を取得してから return することで、read 後に
          ``_invalidate_caches()`` が ``_frontmatter_keys_cache = None`` に
          書き込んでも ``list(None)`` にならない
        """
        with self._lock:
            if self._frontmatter_keys_cache is None:
                self._frontmatter_keys_cache = self._query_frontmatter_keys_from_db()
            snapshot = self._frontmatter_keys_cache
        return list(snapshot)

    def _query_frontmatter_keys_from_db(self) -> list[FrontmatterKeyInfo]:
        """Frontmatter のキー別メタ情報 (型推論 + sample_values + note_count) を DB から取得する."""
        value_counters: dict[str, Counter[str]] = {}
        type_sets: dict[str, set[str]] = {}
        note_counts: dict[str, int] = {}

        with self.connection() as conn:
            rows = conn.execute(
                "SELECT frontmatter FROM notes WHERE json_valid(frontmatter)"
            ).fetchall()

        for row in rows:
            fm = json.loads(row["frontmatter"])
            if not isinstance(fm, dict):
                continue
            _collect_key_info(fm, "", value_counters, type_sets, note_counts)

        # note_counts を「観測された key 全集合」の正準とする (Reviewer D6)。
        # value_counters は parent dict (object) key にはエントリを持たない。
        result: list[FrontmatterKeyInfo] = []
        for key in sorted(note_counts.keys()):
            types = type_sets[key]
            if len(types) == 1:
                vtype = next(iter(types))
            else:
                vtype = "mixed"

            # top-5 頻度降順、同頻度は辞書順。
            # parent dict (object) は value_counters に存在しないので samples=[]。
            counter = value_counters.get(key)
            if counter is None:
                samples: list[str] = []
            else:
                samples_sorted = sorted(
                    counter.items(),
                    key=lambda kv: (-kv[1], kv[0]),
                )[:5]
                samples = [v for v, _ in samples_sorted]

            result.append(
                FrontmatterKeyInfo(
                    key=key,
                    value_type=vtype,
                    sample_values=samples,
                    note_count=note_counts[key],
                )
            )
        return result

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
