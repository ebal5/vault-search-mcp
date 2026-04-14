"""Pydantic models for MCP tool returns and domain errors.

これらのモデルは FastMCP が JSON Schema を自動生成する際に
ツール出力の正確な構造を AI エージェントへ伝達するために使う。
全モデルで `extra="forbid"` を指定し、想定外フィールドの混入を防ぐ。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

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
        description="frontmatter の生データ (任意の YAML 構造)",
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


_TOOL_OUTPUT_MODELS: dict[str, tuple[str, type[BaseModel], bool]] = {
    "vault_search": ("SearchResponse", SearchResponse, False),
    "vault_get_note": ("NoteDetail", NoteDetail, False),
    "vault_recent": ("RecentNote", RecentNote, True),
    "vault_tags": ("TagCount", TagCount, True),
    "vault_folders": ("FolderCount", FolderCount, True),
    "vault_reindex": ("ReindexStats", ReindexStats, False),
    "vault_stats": ("VaultStats", VaultStats, False),
}

_TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "vault_search": {
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
    "vault_get_note": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    "vault_recent": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20},
            "folder": {"type": ["string", "null"]},
        },
    },
    "vault_tags": {"type": "object", "properties": {}},
    "vault_folders": {"type": "object", "properties": {}},
    "vault_reindex": {
        "type": "object",
        "properties": {"force": {"type": "boolean", "default": False}},
    },
    "vault_stats": {"type": "object", "properties": {}},
}

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "vault_search": "Vault 内のノートを全文検索する。",
    "vault_get_note": "指定パスのノート全文とメタデータを取得する。",
    "vault_recent": "最近更新されたノート一覧を取得する。",
    "vault_tags": "全タグとその使用回数を返す。",
    "vault_folders": "フォルダ構造とノート数を返す。",
    "vault_reindex": "インデックスを再構築する。",
    "vault_stats": "インデックスの統計情報を返す。",
}


def build_schema_payload(index: VaultIndex) -> dict[str, Any]:
    """AI エージェント向けに全ツールの入出力スキーマと frontmatter キー一覧を集約."""
    tools: dict[str, dict[str, Any]] = {}
    for tool_name, (_label, model_cls, is_list) in _TOOL_OUTPUT_MODELS.items():
        schema = model_cls.model_json_schema()
        output_schema = {"type": "array", "items": schema} if is_list else schema
        tools[tool_name] = {
            "description": _TOOL_DESCRIPTIONS[tool_name],
            "input_schema": _TOOL_INPUT_SCHEMAS[tool_name],
            "output_schema": output_schema,
        }
    return {
        "tools": tools,
        "frontmatter_keys": index.list_frontmatter_keys(),
    }
