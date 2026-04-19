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
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .exceptions import NoteNotFoundError
from .indexer import VaultIndex
from .mcp_contract import (
    _FOLDER_DESCRIPTION,
    TOOL_SPECS,
    inject_rich_output_schemas,
)
from .resources import build_schema_payload
from .schemas import (
    FolderCount,
    NoteDetail,
    RecentNote,
    SearchHit,
    SearchResponse,
    TagCount,
)
from .stats import ReindexStats, VaultStats
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


def _with_folder_description(fn):
    """docstring の ``{FOLDER_DESCRIPTION}`` placeholder を ``_FOLDER_DESCRIPTION``
    の本文で置換する (Issue #37 — SSOT).

    ``_FOLDER_DESCRIPTION`` は ``mcp_contract._FOLDER_DESCRIPTION`` を単一
    ソースとし、``Field(description=...)`` (MCP inputSchema 経由) と docstring
    (``inspect.getdoc`` / ``help`` / FastMCP description 経由) の両方でこの値を
    共有する。backslash escape 差で drift しないよう、docstring には
    ``{FOLDER_DESCRIPTION}`` という placeholder を書き、本 decorator で runtime
    に差し替える。``@mcp.tool()`` より内側 (decorator スタックの下) で適用する
    ことで、FastMCP が ``__doc__`` を読む時点で既に展開済みにする。
    """
    if fn.__doc__ and "{FOLDER_DESCRIPTION}" in fn.__doc__:
        fn.__doc__ = fn.__doc__.replace("{FOLDER_DESCRIPTION}", _FOLDER_DESCRIPTION)
    return fn


@mcp.tool(annotations=TOOL_SPECS["vault_search"].annotations)
@_with_folder_description
def vault_search(
    query: str,
    tags: list[str] | None = None,
    folder: Annotated[str | None, Field(description=_FOLDER_DESCRIPTION)] = None,
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
        folder: {FOLDER_DESCRIPTION}
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
        常に plain dict を返す ({"tier", "total", "truncated", "results": [dict]})。
        ``truncated`` は結果配列が内部上限 (現在 500 件) で打ち切られた状態を
        true で示す。true のとき offset>=500 は空配列を返すため、クエリを絞るか
        tags / folder / metadata_filter を追加して 500 件以下に収めてから
        ページングを続けること。
        ``total==0`` かつ ``metadata_filter`` 指定時のみ、追加で
        ``metadata_filter_diagnostics`` (list) を含む。各要素は filter に使った
        キーの存在可否 (``key_present_in_index``) と観測値サンプル
        (``observed_values_sample``) を示し、「値が全件不一致」か「キー欠落」か
        をエージェントが区別する助けになる (Issue #80)。
        構造の詳細 (SearchResponse の rich JSON Schema) は ``schema://tools``
        リソースの ``tools.vault_search.output_schema`` を参照。ツール戻り型を
        Union にすると FastMCP が structured content を ``{"result": ...}`` で
        ラップしてしまうため、dict 統一で回避している。
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
        truncated=raw.get("truncated", False),
        results=[SearchHit(**hit) for hit in raw["results"]],
        metadata_filter_diagnostics=raw.get("metadata_filter_diagnostics"),
    ).model_dump(mode="json", exclude_none=True)


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
@_with_folder_description
def vault_recent(
    limit: int = 20,
    offset: int = 0,
    folder: Annotated[str | None, Field(description=_FOLDER_DESCRIPTION)] = None,
) -> dict[str, Any]:
    """最近更新されたノート一覧を取得する。

    Args:
        limit: 最大返却件数（デフォルト 20, 1-500）。
        offset: ページング用の開始位置（デフォルト 0, >=0）。vault_search と同じ
                意味論で、最近更新順 (file_mtime DESC) の先頭から offset 件を
                スキップして limit 件返す。
        folder: {FOLDER_DESCRIPTION}

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

    通常はファイル監視 (VaultWatcher) が差分更新するため手動実行は不要。
    インデックス破損の疑いがある場合や、Vault の大規模変更後に用いる。

    Args:
        force: 再構築の範囲を制御する。

            ``force=False`` (デフォルト — 差分更新):
                既存 DB を保持したまま、各ファイルの mtime を DB 内レコードと比較し、
                変更があったファイルのみ UPSERT、消失ファイルを DELETE する。
                変更がなければ skip カウントが増えるだけで副作用は最小。
                idempotent であり通常はこちらで十分。

            ``force=True`` (全件リビルド):
                DB の既存レコードを無視して全 .md ファイルを再パースし直す。
                notes テーブルが実質全件 UPSERT で置換される。
                インデックス破損の疑いがある場合や大規模な Vault 再編成後に使う。

    副作用:
        どちらの場合も ``vault 本体の .md ファイルは一切 touch しない``。
        更新されるのは ``.vault-search.db`` (派生インデックス DB) のみ。
        ``destructiveHint=False`` を明示しているのはこの理由による。

    Returns:
        dict: ReindexStats に相当する flat JSON
            (``added`` / ``updated`` / ``deleted`` / ``skipped`` / ``errors`` /
            ``watcher_failure_count`` / ``last_watcher_error_at``)。
            末尾 2 つは VaultWatcher が差分更新で失敗した累計 (#39) を返す。
            ``--no-watch`` で watcher 無効の場合と起動以降失敗ゼロの場合は
            それぞれ ``0`` / ``null``。
    """
    stats = _get_index().build_index(force=force)
    watcher_stats = _watcher.failure_stats() if _watcher is not None else {}
    return ReindexStats(**stats, **watcher_stats).model_dump(mode="json")


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

    # MCP サーバー起動（stdio）
    # shutdown / 例外どちらの経路でも watcher.stop() で Observer スレッドを
    # 明示停止し、プロセス終了時のリソースリークを防ぐ (#39)。
    # watcher.start() を try 内に置くことで、start() 自体が例外を投げた場合にも
    # finally が実行され stop() が呼ばれることを保証する。
    logger.info("Starting MCP server (stdio)")
    try:
        if not args.no_watch:
            _watcher = VaultWatcher(_index)
            _watcher.start()
        mcp.run(transport="stdio")
    finally:
        if _watcher is not None:
            _watcher.stop()


if __name__ == "__main__":
    main()
