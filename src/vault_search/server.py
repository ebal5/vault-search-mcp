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

from .exceptions import NoteNotFoundError
from .indexer import VaultIndex
from .mcp_contract import (
    TOOL_SPECS,
    build_schema_payload,
    inject_rich_output_schemas,
)
from .schemas import (
    FolderCount,
    NoteDetail,
    RecentNote,
    ReindexStats,
    SearchHit,
    SearchResponse,
    TagCount,
    VaultStats,
)
from .validation import normalize_folder, validate_pagination
from .watcher import VaultWatcher

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

# MCP tool annotations は ``TOOL_SPECS`` をカノニカルソースとする
# (mcp_contract.py: issue #22 + review round 1 の整理を参照)。
# schema://tools リソースと MCP tools/list の両経路で同一メタデータを公開し、
# server.py 側で定数を重複定義しない。


@mcp.tool(annotations=TOOL_SPECS["vault_search"].annotations)
def vault_search(
    query: str,
    tags: list[str] | None = None,
    folder: str | None = None,
    limit: int = 20,
    offset: int = 0,
    metadata_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Vault 内のノートを全文検索する。

    3段階プログレッシブ検索:
    - Tier 0: 完全キャッシュヒット (~0ms)
    - Tier 1: ファジーキャッシュ — 類似クエリの結果を再利用 (~1ms)
    - Tier 2: FTS5 全文検索 — trigram で日英両対応 (~10-100ms)

    Args:
        query: 検索クエリ。スペース区切りで AND 検索。日本語・英語混在可。
        tags: タグフィルタ（例: ["project/hermes-agent", "status/decided"]）。全て AND。
        folder: フォルダプレフィックスフィルタ（例: "Projects/Hermes Agent"）。
            末尾 '/' および '\\' 区切りは自動で正規化される（例: "Projects/" → "Projects"）。
            スラッシュのみの入力（'/', '//', '\\\\'）はフィルタなし（= 全件）として扱う。
        limit: 最大返却件数（デフォルト 20）。
        offset: 開始位置（ページネーション用）。
        metadata_filter: frontmatter プロパティでの AND フィルタ。
            例: ``{"status": "active", "priority": {"in": ["high", "low"]}}``。
            対応演算子: 暗黙 eq (str 値) / ``{"ne": str}`` / ``{"in": list[str]}``。
            リスト型 frontmatter 値は「含む」判定 (tags と同様)。
            unknown frontmatter key を指定すると ValidationError
            (did_you_mean 付き) を返す (Issue #19)。複数 unknown key を同時に
            指定した場合は 1 回の ValidationError にまとめて報告され、各キーの
            did_you_mean 候補は ``err.unknown_keys[key]`` を参照する (Issue #123)。
            検証優先順位は: 識別子構造エラー (malformed dot 等) → unknown key
            (batch) → operator/value エラー (fail-first)。identifier エラーと
            unknown key が混在する場合、identifier エラーが先に報告される。

    副作用: 読み取り専用。内部的に query cache を更新するが vault / DB への書き込みは無し。

    Returns:
        常に plain dict を返す ({"tier", "total", "results": [dict]})。
        構造の詳細 (SearchResponse の rich JSON Schema) は ``schema://tools``
        リソースの ``tools.vault_search.output_schema`` を参照。
        ツール戻り型を Union にすると FastMCP が structured content を
        ``{"result": ...}`` でラップしてしまうため、dict 統一で回避している。
    """
    validate_pagination(limit, offset)
    raw = _get_index().search(
        query,
        tags=tags,
        folder=normalize_folder(folder) if folder is not None else None,
        metadata_filter=metadata_filter,
        limit=limit,
        offset=offset,
    )
    return SearchResponse(
        tier=raw["tier"],
        total=raw["total"],
        results=[SearchHit(**hit) for hit in raw["results"]],
    ).model_dump(mode="json")


@mcp.tool(annotations=TOOL_SPECS["vault_get_note"].annotations)
def vault_get_note(path: str) -> dict[str, Any]:
    """指定パスのノート全文とメタデータを取得する。

    Args:
        path: Vault ルートからの相対パス（例: "Projects/Hermes Agent/Vault連携方針.md"）

    副作用: 読み取り専用 (vault / DB 書き込み無し)。

    Returns:
        常に plain dict を返す。構造の詳細 (NoteDetail の rich JSON Schema) は
        ``schema://tools`` リソースの ``tools.vault_get_note.output_schema``
        を参照。戻り型統一の理由は ``vault_search`` 参照。

    Raises:
        NoteNotFoundError: 指定された path がインデックスに存在しない場合。
    """
    result = _get_index().get_note(path)
    if result is None:
        raise NoteNotFoundError(path)
    return NoteDetail(**result).model_dump(mode="json")


@mcp.tool(annotations=TOOL_SPECS["vault_recent"].annotations)
def vault_recent(
    limit: int = 20,
    offset: int = 0,
    folder: str | None = None,
) -> dict[str, Any]:
    """最近更新されたノート一覧を取得する。

    Args:
        limit: 最大返却件数（デフォルト 20, 1-500）。
        offset: ページング用の開始位置（デフォルト 0, >=0）。vault_search と同じ
                意味論で、最近更新順 (file_mtime DESC) の先頭から offset 件を
                スキップして limit 件返す。
        folder: フォルダプレフィックスで絞り込み（例: "Research"）。
            末尾 '/' および '\\' 区切りは自動で正規化される（例: "Research/" → "Research"）。
            スラッシュのみの入力（'/', '//', '\\\\'）はフィルタなし（= 全件）として扱う。

    副作用: 読み取り専用 (vault / DB 書き込み無し)。

    Returns:
        常に plain dict を envelope 形式で返す (``{"notes": [dict, ...]}``)。
        file_mtime 降順。構造の詳細 (RecentNote の rich JSON Schema) は
        ``schema://tools`` リソースの ``tools.vault_recent.output_schema``
        を参照。list 戻り型だと FastMCP が ``{"result": [...]}`` にラップ
        してしまうため dict envelope に統一している。
    """
    validate_pagination(limit, offset)
    rows = _get_index().recent_notes(
        limit=limit,
        offset=offset,
        folder=normalize_folder(folder) if folder is not None else None,
    )
    notes = [RecentNote(**note).model_dump(mode="json") for note in rows]
    return {"notes": notes}


@mcp.tool(annotations=TOOL_SPECS["vault_tags"].annotations)
def vault_tags() -> dict[str, Any]:
    """全タグとその使用回数を返す。出現回数降順。

    副作用: 読み取り専用 (vault / DB 書き込み無し)。

    Returns:
        envelope dict (``{"tags": [{"tag": ..., "count": ...}, ...]}``)。
        frontmatter.tags と本文インライン #tag の両方が集計対象。
        戻り型統一の理由は ``vault_recent`` 参照。
    """
    return {"tags": [TagCount(**row).model_dump(mode="json") for row in _get_index().list_tags()]}


@mcp.tool(annotations=TOOL_SPECS["vault_folders"].annotations)
def vault_folders() -> dict[str, Any]:
    """フォルダ構造とノート数を返す。

    副作用: 読み取り専用 (vault / DB 書き込み無し)。

    Returns:
        envelope dict (``{"folders": [{"folder": ..., "count": ...}, ...]}``)。
        フォルダパス昇順。ルート直下のノートは folder='' に集約され、
        SearchHit/RecentNote と同じ表現。この値はそのまま
        vault_search/vault_recent の folder 引数に渡せる。
    """
    return {
        "folders": [
            FolderCount(**row).model_dump(mode="json") for row in _get_index().list_folders()
        ]
    }


@mcp.tool(annotations=TOOL_SPECS["vault_reindex"].annotations)
def vault_reindex(force: bool = False) -> dict[str, Any]:
    """インデックスを再構築する。

    通常はファイル監視が差分更新するため手動実行は不要。
    Vault の大規模変更後やインデックス破損時に使う。

    Args:
        force: True なら全件リビルド。False なら mtime ベースの差分更新。

    副作用: `.vault-search.db` (派生インデックス) のみを更新。
        vault 本体 (.md ファイル) は一切 touch しない。

    Returns:
        dict: ReindexStats に相当する flat JSON (added / updated / deleted / skipped / errors)。
    """
    return ReindexStats(**_get_index().build_index(force=force)).model_dump(mode="json")


@mcp.tool(annotations=TOOL_SPECS["vault_stats"].annotations)
def vault_stats() -> dict[str, Any]:
    """インデックスの統計情報を返す。

    副作用: 読み取り専用 (vault / DB 書き込み無し)。

    Returns:
        dict: VaultStats に相当する flat JSON
            (total_notes / db_size_bytes / db_size_mb / vault_root)。
    """
    return VaultStats(**_get_index().stats()).model_dump(mode="json")


# ---------------------------------------------------------------------------
# MCP outputSchema injection
# ---------------------------------------------------------------------------
# 実体 + TODO(FastMCP) コメントは mcp_contract.inject_rich_output_schemas を参照。

inject_rich_output_schemas(mcp)


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------


@mcp.resource("schema://tools")
def schema_resource() -> dict[str, Any]:
    """全ツールの入出力スキーマと frontmatter キー一覧を返す.

    schema://tools の output_schema と MCP tools/list の outputSchema は
    ``TOOL_ENTRIES`` を共通のカノニカルソースとして同一内容になる
    (``mcp_contract.inject_rich_output_schemas`` 参照)。
    """
    return build_schema_payload(_get_index().list_frontmatter_keys())


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
