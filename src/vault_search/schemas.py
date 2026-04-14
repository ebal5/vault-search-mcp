"""Pydantic models for MCP tool returns and domain errors.

これらのモデルは FastMCP が JSON Schema を自動生成する際に
ツール出力の正確な構造を AI エージェントへ伝達するために使う。
全モデルで `extra="forbid"` を指定し、想定外フィールドの混入を防ぐ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .validation import validate_identifier

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

    path: str = Field(default="", description="Vault ルートからの相対パス (例: 'Notes/foo.md')")
    title: str = Field(default="", description="ノートタイトル (frontmatter.title または最初の H1)")
    folder: str = Field(
        default="", description="所属フォルダ (Vault ルートからの相対、ルート直下は '')"
    )
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

    path: str = Field(default="", description="Vault ルートからの相対パス")
    title: str = Field(default="", description="ノートタイトル")
    folder: str = Field(default="", description="所属フォルダ")
    tags: list[str] = Field(default_factory=list, description="タグ一覧")
    aliases: list[str] = Field(
        default_factory=list, description="frontmatter.aliases 由来の別名一覧"
    )
    created_at: str = Field(default="", description="作成日時 (frontmatter 由来)")
    modified_at: str = Field(default="", description="更新日時 (frontmatter 由来)")
    content: str = Field(
        default="",
        description="frontmatter を除いた Markdown 本文 (前後空白は trim 済み)",
    )
    frontmatter: dict[str, Any] = Field(
        default_factory=dict,
        description="frontmatter の生データ (任意の YAML 構造)",
    )


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


class RecentNote(BaseModel):
    """`vault_recent` ツールのレスポンス要素: 最近更新ノートのメタデータ."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="", description="Vault ルートからの相対パス")
    title: str = Field(default="", description="ノートタイトル")
    folder: str = Field(default="", description="所属フォルダ")
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
        description="フォルダパス (Vault ルートからの相対)。ルート直下は '(root)'",
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
    """単一 MCP ツールの description / input_schema / output_model を束ねる仕様."""

    description: str
    input_schema: dict[str, Any]
    output_model: type[BaseModel]
    output_is_list: bool = False


_TOOL_SPECS: dict[str, _ToolSchemaSpec] = {
    "vault_search": _ToolSchemaSpec(
        description="Vault 内のノートを全文検索する。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索クエリ"},
                "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                "folder": {"type": ["string", "null"]},
                "limit": {"type": "integer", "default": 20},
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["query"],
        },
        output_model=SearchResponse,
    ),
    "vault_get_note": _ToolSchemaSpec(
        description="指定パスのノート全文とメタデータを取得する。",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        output_model=NoteDetail,
    ),
    "vault_recent": _ToolSchemaSpec(
        description="最近更新されたノート一覧を取得する。",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "folder": {"type": ["string", "null"]},
            },
        },
        output_model=RecentNote,
        output_is_list=True,
    ),
    "vault_tags": _ToolSchemaSpec(
        description="全タグとその使用回数を返す。",
        input_schema={"type": "object", "properties": {}},
        output_model=TagCount,
        output_is_list=True,
    ),
    "vault_folders": _ToolSchemaSpec(
        description="フォルダ構造とノート数を返す。",
        input_schema={"type": "object", "properties": {}},
        output_model=FolderCount,
        output_is_list=True,
    ),
    "vault_reindex": _ToolSchemaSpec(
        description="インデックスを再構築する。",
        input_schema={
            "type": "object",
            "properties": {"force": {"type": "boolean", "default": False}},
        },
        output_model=ReindexStats,
    ),
    "vault_stats": _ToolSchemaSpec(
        description="インデックスの統計情報を返す。",
        input_schema={"type": "object", "properties": {}},
        output_model=VaultStats,
    ),
}


def _build_tool_entry(spec: _ToolSchemaSpec) -> dict[str, Any]:
    """1 ツール分の {description, input_schema, output_schema} エントリを生成."""
    schema = spec.output_model.model_json_schema()
    output_schema = {"type": "array", "items": schema} if spec.output_is_list else schema
    return {
        "description": spec.description,
        "input_schema": spec.input_schema,
        "output_schema": output_schema,
    }


def apply_field_mask(
    model_cls: type[BaseModel],
    data: dict[str, Any],
    fields: list[str] | None,
) -> dict[str, Any]:
    """fields 指定に基づいて data を subset する.

    fields=None → data をそのまま返却 (後方互換)
    fields=[] → ValidationError
    fields に存在しないフィールド名が含まれる → ValidationError
    それ以外 → data から fields だけを抜き出した dict を返す
      (モデル構築は呼び出し側で行い、欠落フィールドはモデルのデフォルト値で埋まる)
    """
    from .validation import ValidationError

    if fields is None:
        return data
    if not isinstance(fields, list):
        raise ValidationError(f"fields must be a list, got {type(fields).__name__}")
    if len(fields) == 0:
        raise ValidationError("fields must not be empty; pass null to return all fields")
    allowed = set(model_cls.model_fields.keys())
    for name in fields:
        validate_identifier(name, kind="field name", max_len=64)
        if name not in allowed:
            raise ValidationError(f"unknown field name: {name!r} (allowed: {sorted(allowed)})")
    return {k: v for k, v in data.items() if k in set(fields)}


def build_schema_payload(index: VaultIndex) -> dict[str, Any]:
    """AI エージェント向けに全ツールの入出力スキーマと frontmatter キー一覧を集約."""
    return {
        "tools": {name: _build_tool_entry(spec) for name, spec in _TOOL_SPECS.items()},
        "frontmatter_keys": index.list_frontmatter_keys(),
    }
