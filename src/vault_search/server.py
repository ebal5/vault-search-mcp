"""vault-search-mcp: FastMCP サーバー.

Obsidian Vault の高速構造化検索を MCP プロトコルで提供する。

Usage:
    # stdio モード（Hermes Agent から呼ぶ標準形式）
    python -m vault_search.server --vault /path/to/vault

    # 環境変数でも指定可能
    VAULT_ROOT=/path/to/vault python -m vault_search.server

    # DB パスのカスタマイズ（デフォルトは vault_root/.vault-search.db）
    python -m vault_search.server --vault /path/to/vault --db /tmp/vault-search.db
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .indexer import VaultIndex, VaultWatcher
from .schemas import (
    FolderCount,
    NoteDetail,
    NoteNotFoundError,
    RecentNote,
    ReindexStats,
    SearchHit,
    SearchResponse,
    TagCount,
    VaultStats,
    apply_field_mask,
    build_schema_payload,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# グローバルインスタンス（FastMCP のツール関数から参照）
# ---------------------------------------------------------------------------

_index: VaultIndex | None = None
_watcher: VaultWatcher | None = None


def _get_index() -> VaultIndex:
    if _index is None:
        raise RuntimeError("VaultIndex not initialized")
    return _index


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("vault-search")


@mcp.tool()
def vault_search(
    query: str,
    tags: list[str] | None = None,
    folder: str | None = None,
    limit: int = 20,
    offset: int = 0,
    fields: list[str] | None = None,
) -> SearchResponse:
    """Vault 内のノートを全文検索する。

    3段階プログレッシブ検索:
    - Tier 0: 完全キャッシュヒット (~0ms)
    - Tier 1: ファジーキャッシュ — 類似クエリの結果を再利用 (~1ms)
    - Tier 2: FTS5 全文検索 — trigram で日英両対応 (~10-100ms)

    Args:
        query: 検索クエリ。スペース区切りで AND 検索。日本語・英語混在可。
        tags: タグフィルタ（例: ["project/hermes-agent", "status/decided"]）。全て AND。
        folder: フォルダプレフィックスフィルタ（例: "Projects/Hermes Agent"）。
        limit: 最大返却件数（デフォルト 20）。
        offset: 開始位置（ページネーション用）。
        fields: 返却フィールドを限定 (例: ["path", "title"])。None で全フィールド。
                空リストまたは不正名は ValidationError。context window 節約用。

    Returns:
        SearchResponse: tier (どのキャッシュ段でヒットしたか), total (フィルタ後の総件数),
        results (limit/offset 適用後の SearchHit 一覧)。
    """
    raw = _get_index().search(query, tags=tags, folder=folder, limit=limit, offset=offset)
    return SearchResponse(
        tier=raw["tier"],
        total=raw["total"],
        results=[SearchHit(**apply_field_mask(SearchHit, hit, fields)) for hit in raw["results"]],
    )


@mcp.tool()
def vault_get_note(path: str, fields: list[str] | None = None) -> NoteDetail:
    """指定パスのノート全文とメタデータを取得する。

    Args:
        path: Vault ルートからの相対パス（例: "Projects/Hermes Agent/Vault連携方針.md"）
        fields: 返却フィールドを限定 (例: ["path", "title"])。None で全フィールド。
                空リストまたは不正名は ValidationError。context window 節約用。

    Returns:
        NoteDetail: 本文 (frontmatter 除去済み) と全メタデータを含む構造体。

    Raises:
        NoteNotFoundError: 指定された path がインデックスに存在しない場合。
    """
    result = _get_index().get_note(path)
    if result is None:
        raise NoteNotFoundError(path)
    return NoteDetail(**apply_field_mask(NoteDetail, result, fields))


@mcp.tool()
def vault_recent(
    limit: int = 20,
    folder: str | None = None,
    fields: list[str] | None = None,
) -> list[RecentNote]:
    """最近更新されたノート一覧を取得する。

    Args:
        limit: 最大返却件数（デフォルト 20）
        folder: フォルダプレフィックスで絞り込み（例: "Research"）
        fields: 返却フィールドを限定 (例: ["path"])。None で全フィールド。
                空リストまたは不正名は ValidationError。context window 節約用。

    Returns:
        file_mtime 降順の RecentNote リスト。本文・スニペットは含まれない。
    """
    return [
        RecentNote(**apply_field_mask(RecentNote, note, fields))
        for note in _get_index().recent_notes(limit=limit, folder=folder)
    ]


@mcp.tool()
def vault_tags() -> list[TagCount]:
    """全タグとその使用回数を返す。出現回数降順。

    Returns:
        TagCount のリスト。frontmatter.tags と本文インライン #tag の両方が集計対象。
    """
    return [TagCount(**row) for row in _get_index().list_tags()]


@mcp.tool()
def vault_folders() -> list[FolderCount]:
    """フォルダ構造とノート数を返す。

    Returns:
        フォルダパス昇順の FolderCount リスト。ルート直下のノートは folder='(root)' に集約。
    """
    return [FolderCount(**row) for row in _get_index().list_folders()]


@mcp.tool()
def vault_reindex(force: bool = False) -> ReindexStats:
    """インデックスを再構築する。

    通常はファイル監視が差分更新するため手動実行は不要。
    Vault の大規模変更後やインデックス破損時に使う。

    Args:
        force: True なら全件リビルド。False なら mtime ベースの差分更新。

    Returns:
        ReindexStats: added / updated / deleted / skipped / errors の件数内訳。
    """
    return ReindexStats(**_get_index().build_index(force=force))


@mcp.tool()
def vault_stats() -> VaultStats:
    """インデックスの統計情報を返す。

    Returns:
        VaultStats: ノート総数, DB サイズ (bytes / MB), Vault ルート絶対パス。
    """
    return VaultStats(**_get_index().stats())


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------


@mcp.resource("schema://tools")
def schema_resource() -> dict[str, Any]:
    """全ツールの入出力スキーマと frontmatter キー一覧を返す."""
    return build_schema_payload(_get_index())


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="vault-search-mcp server")
    parser.add_argument(
        "--vault",
        default=os.environ.get("VAULT_ROOT", ""),
        help="Obsidian Vault のルートパス (env: VAULT_ROOT)",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("VAULT_SEARCH_DB", ""),
        help="SQLite DB パス (デフォルト: vault_root/.vault-search.db)",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        default=os.environ.get("VAULT_SEARCH_NO_WATCH", "") == "1",
        help="ファイル監視を無効化",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("VAULT_SEARCH_LOG_LEVEL", "INFO"),
        help="ログレベル (DEBUG, INFO, WARNING, ERROR)",
    )
    args = parser.parse_args()

    if not args.vault:
        print("Error: --vault or VAULT_ROOT is required", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    vault_root = Path(args.vault).resolve()
    if not vault_root.is_dir():
        print(f"Error: Vault directory not found: {vault_root}", file=sys.stderr)
        sys.exit(1)

    db_path = Path(args.db) if args.db else None

    # グローバルインスタンス初期化
    global _index, _watcher
    _index = VaultIndex(vault_root, db_path=db_path)

    # 初回インデックス構築
    logger.info("Building initial index for: %s", vault_root)
    stats = _index.build_index()
    logger.info("Initial index: %s", stats)

    # ファイル監視開始
    if not args.no_watch:
        _watcher = VaultWatcher(_index)
        _watcher.start()

    # MCP サーバー起動（stdio）
    logger.info("Starting MCP server (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
