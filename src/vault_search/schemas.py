"""Pydantic models for MCP tool returns and domain errors.

これらのモデルは FastMCP が JSON Schema を自動生成する際に
ツール出力の正確な構造を AI エージェントへ伝達するために使う。
全モデルで `extra="forbid"` を指定し、想定外フィールドの混入を防ぐ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from .indexer import VaultIndex

# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class VaultSearchError(Exception):
    """vault-search-mcp ドメインの基底例外."""


class NoteNotFoundError(VaultSearchError):
    """指定された path のノートがインデックスに存在しない."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Note not found: {path}")
        self.path = path


# ---------------------------------------------------------------------------
# Search responses
# ---------------------------------------------------------------------------


class SearchHit(BaseModel):
    """検索ヒット 1 件分のメタデータ + スニペット."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Vault ルートからの相対パス (例: 'Notes/foo.md')")
    title: str = Field(description="ノートタイトル (frontmatter.title または最初の H1)")
    folder: str = Field(description="所属フォルダ (Vault ルートからの相対、ルート直下は '')")
    tags: list[str] = Field(
        default_factory=list,
        description="タグ一覧 (frontmatter.tags + 本文インライン #tag)",
    )
    snippet: str = Field(
        default="",
        description=(
            "マッチ位置の抜粋。'>>>' / '<<<' でハイライト箇所を囲む。"
            "3文字未満クエリのフォールバック時は空文字"
        ),
    )
    score: float = Field(
        default=0.0,
        description="FTS5 rank スコア。値が小さいほど関連度が高い (BM25 の負号付き)",
    )
    created_at: str = Field(
        default="",
        description="frontmatter から推定された作成日時。文字列のまま返す (ISO8601 とは限らない)",
    )
    modified_at: str = Field(
        default="",
        description="frontmatter から推定された更新日時。文字列のまま返す",
    )


class SearchResponse(BaseModel):
    """`vault_search` ツールのレスポンス."""

    model_config = ConfigDict(extra="forbid")

    tier: Literal[0, 1, 2] = Field(
        description=(
            "ヒットしたキャッシュ段。0=完全一致キャッシュ, 1=ファジーキャッシュ, 2=FTS5 検索"
        ),
    )
    total: int = Field(
        description="フィルタ後の総件数 (limit/offset 適用前)",
    )
    results: list[SearchHit] = Field(
        default_factory=list,
        description="limit/offset でスライスされた検索ヒット一覧",
    )


# ---------------------------------------------------------------------------
# Single note
# ---------------------------------------------------------------------------


class NoteDetail(BaseModel):
    """`vault_get_note` ツールのレスポンス: ノート全文 + メタデータ."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Vault ルートからの相対パス")
    title: str = Field(description="ノートタイトル")
    folder: str = Field(description="所属フォルダ")
    tags: list[str] = Field(default_factory=list, description="タグ一覧")
    aliases: list[str] = Field(
        default_factory=list, description="frontmatter.aliases 由来の別名一覧"
    )
    created_at: str = Field(default="", description="作成日時 (frontmatter 由来)")
    modified_at: str = Field(default="", description="更新日時 (frontmatter 由来)")
    content: str = Field(
        description="frontmatter を除いた Markdown 本文 (前後空白は trim 済み)",
    )
    frontmatter: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "frontmatter の正規化済みデータ。スカラー値 (int/float/bool/date/datetime) は "
            "index 時に文字列化されている: "
            '5→"5" / true→"true" / false→"false" / '
            '2024-01-15→"2024-01-15" / datetime→ISO 8601 "T" 区切り文字列。'
            "None (YAML null) と str は保持。list/dict は要素を再帰的に正規化した形で返る。"
            "YAML 原文の型情報は保持されない (metadata_filter との str 比較を一貫させるため)。"
        ),
    )


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


class RecentNote(BaseModel):
    """`vault_recent` ツールのレスポンス要素: 最近更新ノートのメタデータ."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Vault ルートからの相対パス")
    title: str = Field(description="ノートタイトル")
    folder: str = Field(description="所属フォルダ")
    tags: list[str] = Field(default_factory=list, description="タグ一覧")
    created_at: str = Field(default="", description="作成日時")
    modified_at: str = Field(default="", description="更新日時")


class TagCount(BaseModel):
    """`vault_tags` ツールのレスポンス要素: タグと出現回数."""

    model_config = ConfigDict(extra="forbid")

    tag: str = Field(description="タグ名 (先頭 '#' なし)")
    count: int = Field(description="このタグが付与されたノート数")


class FolderCount(BaseModel):
    """`vault_folders` ツールのレスポンス要素: フォルダと所属ノート数."""

    model_config = ConfigDict(extra="forbid")

    folder: str = Field(
        description=(
            "フォルダパス (Vault ルートからの相対)。ルート直下は '' "
            "(SearchHit/RecentNote/NoteDetail.folder と同じ表現)。"
            "この値はそのまま vault_search(folder=...) / vault_recent(folder=...) に渡せる"
        ),
    )
    count: int = Field(description="このフォルダ直下のノート数")


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


class ReindexStats(BaseModel):
    """`vault_reindex` ツールのレスポンス: 処理件数の内訳."""

    model_config = ConfigDict(extra="forbid")

    added: int = Field(description="新規追加されたノート数")
    updated: int = Field(description="更新されたノート数 (mtime 進行)")
    deleted: int = Field(description="削除されたノート数 (ファイル消失)")
    skipped: int = Field(description="mtime 変化なしでスキップされたノート数")
    errors: int = Field(description="パース失敗したノート数")


class VaultStats(BaseModel):
    """`vault_stats` ツールのレスポンス: インデックス全体の統計."""

    model_config = ConfigDict(extra="forbid")

    total_notes: int = Field(description="インデックス済みノート総数")
    db_size_bytes: int = Field(description="SQLite DB ファイルのサイズ (バイト)")
    db_size_mb: float = Field(description="SQLite DB ファイルのサイズ (MB, 小数2桁)")
    vault_root: str = Field(description="Vault ルートの絶対パス")


# ---------------------------------------------------------------------------
# Schema introspection payload (schema://tools resource)
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
_LIMIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 500,
    "default": 20,
    "description": "最大返却件数 (1-500)。上限超過は ValidationError。",
}


_OFFSET_INPUT_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 0,
    "default": 0,
    "description": "ページング用の開始位置 (>=0)。負値は ValidationError。",
}


# MCP ``ToolAnnotations`` (issue #22 + review round 1, reviewers A-D).
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


_TOOL_SPECS: dict[str, _ToolSchemaSpec] = {
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


# ``_TOOL_ENTRIES`` は各ツールの (description / input_schema / output_schema) を
# 束ねる唯一のカノニカルソース。以下 2 経路から参照される:
#   1. ``build_schema_payload`` → ``schema://tools`` リソース (AI エージェント向け自己記述)
#   2. ``server._inject_rich_output_schemas`` → MCP ``tools/list`` の ``outputSchema``
# FastMCP は ``dict[str, Any]`` 戻り型から rich schema を自動生成できないため、
# 登録後に本エントリの ``output_schema`` を手動で差し込んで両経路の出力を一致させる。
_TOOL_ENTRIES: dict[str, dict[str, Any]] = {
    name: _build_tool_entry(spec, name) for name, spec in _TOOL_SPECS.items()
}


def build_schema_payload(index: VaultIndex) -> dict[str, Any]:
    """AI エージェント向けに全ツールの入出力スキーマと frontmatter キー一覧を集約."""
    return {
        "tools": _TOOL_ENTRIES,
        "frontmatter_keys": index.list_frontmatter_keys(),
    }
