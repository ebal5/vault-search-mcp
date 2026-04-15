"""MCP ツール契約の集約ポイント.

Pydantic モデル (``schemas.py``) から MCP ツールの外形 (description /
input_schema / output_schema / annotations) を組み立て、AI エージェント向け
``schema://tools`` リソースと MCP ``tools/list`` outputSchema の両経路へ
同一 payload を供給する。

このモジュールは FastMCP の内部 API に依存するハック
(``inject_rich_output_schemas``) も抱える。FastMCP が公式に
``@tool(output_schema=...)`` を提供したときの撤去候補を 1 箇所に集める意図。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from .schemas import (
    FolderCount,
    NoteDetail,
    RecentNote,
    ReindexStats,
    SearchResponse,
    TagCount,
    VaultStats,
)
from .validation import LIMIT_MAX

__all__ = [
    "TOOL_ENTRIES",
    "TOOL_SPECS",
    "build_schema_payload",
    "inject_rich_output_schemas",
]


# ---------------------------------------------------------------------------
# Tool spec dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ToolSchemaSpec:
    """単一 MCP ツールの description / input_schema / output_model を束ねる仕様.

    envelope_key を指定した場合、output_schema は
    ``{"type": "object", "properties": {<envelope_key>: {"type": "array",
    "items": output_model.model_json_schema()}}, "required": [<envelope_key>]}``
    という envelope 形になる。これは実 MCP レスポンス
    (``{envelope_key: [...]}``) と合わせるためで、FastMCP が list 戻り型を
    ``{"result": [...]}`` にラップする挙動を回避する server 側の戻り型変更と
    セットで運用する。
    """

    description: str
    input_schema: dict[str, Any]
    output_model: type[BaseModel]
    envelope_key: str | None = None
    annotations: ToolAnnotations | None = None


# ---------------------------------------------------------------------------
# Shared input schema fragments
# ---------------------------------------------------------------------------


_FOLDER_INPUT_SCHEMA: dict[str, Any] = {
    "type": ["string", "null"],
    "description": (
        "フォルダパス (Vault ルートからの相対)。指定したフォルダ自身と"
        "その配下のみを対象とし、同プレフィックス兄弟 ('Projects' 指定で "
        "'Projects Hermes/...' など) は除外される。"
        "vault_folders の結果 (FolderCount.folder) をそのまま渡せる。"
        "root 直下に限定したい場合は現状未サポート (null で全件)。"
    ),
}


# Shared pagination input schemas. ``minimum`` / ``maximum`` are the single
# source of truth the agent sees via ``schema://tools``; the runtime guard in
# ``validation.validate_pagination`` enforces the same bounds server-side.
# ``LIMIT_MAX`` is imported from validation.py so that the agent-facing bound
# and the server-side guard cannot drift.
_LIMIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": LIMIT_MAX,
    "default": 20,
    "description": f"最大返却件数 (1-{LIMIT_MAX})。上限超過は ValidationError。",
}


_OFFSET_INPUT_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 0,
    "default": 0,
    "description": "ページング用の開始位置 (>=0)。負値は ValidationError。",
}


# ---------------------------------------------------------------------------
# Tool annotations (MCP spec)
# ---------------------------------------------------------------------------
#
# Spec-strict mapping:
#   - 読み取り系は ``readOnlyHint=True`` のみ宣言。MCP spec は
#     ``destructiveHint`` / ``idempotentHint`` を "meaningful only when
#     readOnlyHint == false" と定義しているため、読み取り系では None のまま残す
#     (FastMCP は None フィールドを wire 上で落とす)。
#   - ``vault_reindex`` は唯一の writer。``destructiveHint=False`` は意図的:
#     MCP spec の "destructive" は user-facing データの不可逆損失を指し、
#     派生キャッシュ (``.vault-search.db``) の再構築は vault 本体 (``.md``) を
#     touch しないため該当しない。auto-approve UX で誤警告が出ないよう False。
#     ``idempotentHint=True`` は同一入力で同一状態に収束することを示す。
#   - ``openWorldHint=False`` は全ツールでローカル vault のみを扱うため統一。
_READ_ONLY_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_REINDEX_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


# ---------------------------------------------------------------------------
# Tool specs (canonical source for all 7 tools)
# ---------------------------------------------------------------------------


TOOL_SPECS: dict[str, _ToolSchemaSpec] = {
    "vault_search": _ToolSchemaSpec(
        description="Vault 内のノートを全文検索する。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索クエリ"},
                "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                "folder": _FOLDER_INPUT_SCHEMA,
                "limit": _LIMIT_INPUT_SCHEMA,
                "offset": _OFFSET_INPUT_SCHEMA,
                "metadata_filter": {
                    "type": ["object", "null"],
                    "description": (
                        "frontmatter の各キーに対する AND フィルタ条件。"
                        "キーは frontmatter プロパティ名。値は str (暗黙 eq) または "
                        '{"in": list[str]} / {"ne": str}。'
                        '例: {"status": "active", "priority": {"in": ["high"]}}。'
                        "比較値は常に文字列。frontmatter のスカラーは index 時に正規化される: "
                        'int 5→"5" / float 4.5→"4.5" / bool true→"true" false→"false" / '
                        'date 2024-01-15→"2024-01-15" / '
                        "datetime は ISO 8601 の T 区切り "
                        '(e.g. "2024-01-15 14:30:00"→"2024-01-15T14:30:00", '
                        'タイムゾーン付きは "+00:00" 形式)。'
                        "list 要素も再帰的に正規化される (tags: [1,2] なら "
                        '["1","2"] として要素含有判定)。'
                        "YAML null / 存在しないキーは eq / ne どちらにもマッチしない (3 値論理)。"
                        "数値・日付の範囲比較 (gt/lt/gte) は未対応 — 必要なら取得後に"
                        "クライアント側でフィルタすること。"
                    ),
                    "additionalProperties": {
                        "oneOf": [
                            {
                                "type": "string",
                                "description": (
                                    "暗黙 eq: 値との完全一致 (配列フィールドは要素含有)"
                                ),
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "in": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "minItems": 1,
                                    },
                                },
                                "required": ["in"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {"ne": {"type": "string"}},
                                "required": ["ne"],
                                "additionalProperties": False,
                            },
                        ],
                    },
                },
            },
            "required": ["query"],
        },
        output_model=SearchResponse,
        annotations=_READ_ONLY_ANNOTATIONS,
    ),
    "vault_get_note": _ToolSchemaSpec(
        description="指定パスのノート全文とメタデータを取得する。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
        output_model=NoteDetail,
        annotations=_READ_ONLY_ANNOTATIONS,
    ),
    "vault_recent": _ToolSchemaSpec(
        description="最近更新されたノート一覧を取得する。",
        input_schema={
            "type": "object",
            "properties": {
                "limit": _LIMIT_INPUT_SCHEMA,
                "offset": _OFFSET_INPUT_SCHEMA,
                "folder": _FOLDER_INPUT_SCHEMA,
            },
        },
        output_model=RecentNote,
        envelope_key="notes",
        annotations=_READ_ONLY_ANNOTATIONS,
    ),
    "vault_tags": _ToolSchemaSpec(
        description="全タグとその使用回数を返す。",
        input_schema={"type": "object", "properties": {}},
        output_model=TagCount,
        envelope_key="tags",
        annotations=_READ_ONLY_ANNOTATIONS,
    ),
    "vault_folders": _ToolSchemaSpec(
        description="フォルダ構造とノート数を返す。",
        input_schema={"type": "object", "properties": {}},
        output_model=FolderCount,
        envelope_key="folders",
        annotations=_READ_ONLY_ANNOTATIONS,
    ),
    "vault_reindex": _ToolSchemaSpec(
        description="インデックスを再構築する。",
        input_schema={
            "type": "object",
            "properties": {"force": {"type": "boolean", "default": False}},
        },
        output_model=ReindexStats,
        annotations=_REINDEX_ANNOTATIONS,
    ),
    "vault_stats": _ToolSchemaSpec(
        description="インデックスの統計情報を返す。",
        input_schema={"type": "object", "properties": {}},
        output_model=VaultStats,
        annotations=_READ_ONLY_ANNOTATIONS,
    ),
}


def _build_tool_entry(spec: _ToolSchemaSpec, tool_name: str) -> dict[str, Any]:
    item_schema = spec.output_model.model_json_schema()
    if spec.envelope_key is not None:
        # envelope dict: {<envelope_key>: [item, item, ...]}
        output_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                spec.envelope_key: {
                    "type": "array",
                    "items": item_schema,
                    "description": (
                        f"{spec.output_model.__name__} の配列 "
                        "(list 戻り型の FastMCP wrap を回避する envelope)"
                    ),
                },
            },
            "required": [spec.envelope_key],
            "additionalProperties": False,
        }
    else:
        output_schema = item_schema
    entry: dict[str, Any] = {
        "description": spec.description,
        "input_schema": spec.input_schema,
        "output_schema": output_schema,
    }
    if spec.annotations is not None:
        # exclude_none=True で未宣言 hint を落とし、MCP tools/list の wire 形と揃える
        # (spec で意味を持たない readOnly+destructive/idempotent の組を残さない)。
        entry["annotations"] = spec.annotations.model_dump(mode="json", exclude_none=True)
    return entry


# ``TOOL_ENTRIES`` は各ツールの (description / input_schema / output_schema) を
# 束ねる唯一のカノニカルソース。以下 2 経路から参照される:
#   1. ``build_schema_payload`` → ``schema://tools`` リソース (AI エージェント向け自己記述)
#   2. ``inject_rich_output_schemas`` → MCP ``tools/list`` の ``outputSchema``
# FastMCP は ``dict[str, Any]`` 戻り型から rich schema を自動生成できないため、
# 登録後に本エントリの ``output_schema`` を手動で差し込んで両経路の出力を一致させる。
TOOL_ENTRIES: dict[str, dict[str, Any]] = {
    name: _build_tool_entry(spec, name) for name, spec in TOOL_SPECS.items()
}


def build_schema_payload(frontmatter_keys: Iterable[str]) -> dict[str, Any]:
    """AI エージェント向けに全ツールの入出力スキーマと frontmatter キー一覧を集約.

    呼び出し側は ``VaultIndex.list_frontmatter_keys()`` 相当の反復可能オブジェクトを
    渡す。mcp_contract モジュールが indexer に依存しないよう引数化している。
    """
    return {
        "tools": TOOL_ENTRIES,
        "frontmatter_keys": list(frontmatter_keys),
    }


# ---------------------------------------------------------------------------
# MCP outputSchema injection (FastMCP internal-API hack)
# ---------------------------------------------------------------------------
#
# 背景:
#   各ツールは戻り型を ``dict[str, Any]`` に統一している。これは
#   ``SearchResponse | dict[str, Any]`` の Union が FastMCP の wrap_output=True を
#   誘発し structured content が ``{"result": ...}`` にラップされる問題を回避するため。
#
# 副作用:
#   ``dict[str, Any]`` 戻り型から FastMCP が自動生成する outputSchema は
#   ``{"type": "object", "additionalProperties": true}`` 相当の空 schema になり、
#   schema://tools リソースが公開する rich schema と drift する (カノニカルソース 2 つ問題)。
#
# 対応:
#   登録済み Tool の ``fn_metadata.output_schema`` を TOOL_ENTRIES の rich schema に
#   差し替え、MCP tools/list の outputSchema を schema://tools と同じカノニカル形に
#   揃える。``Tool.output_schema`` は cached_property のため、instance dict に
#   直接書き込んでプロパティ評価をバイパスする。
#
# TODO(FastMCP):
#   FastMCP が @tool(output_schema=...) のような公式 API を提供したら移行する。
#   参考: mcp/server/fastmcp/server.py の tool() シグネチャ (2026-04 現在 output_schema なし)。


def inject_rich_output_schemas(mcp: FastMCP) -> None:
    """登録済み MCP ツールの outputSchema を schema://tools と同じ rich schema に差し替える."""
    tool_manager = mcp._tool_manager  # noqa: SLF001 — FastMCP 公式 API 不在のため内部参照
    for tool_name, entry in TOOL_ENTRIES.items():
        tool = tool_manager._tools.get(tool_name)  # noqa: SLF001
        if tool is None:  # pragma: no cover — 実装ミス以外では起きない
            raise RuntimeError(f"Tool not registered: {tool_name}")
        rich_schema = entry["output_schema"]
        tool.fn_metadata.output_schema = rich_schema
        # cached_property をバイパスして MCP list_tools 経路に rich schema を公開
        tool.__dict__["output_schema"] = rich_schema
