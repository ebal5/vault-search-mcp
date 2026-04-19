"""Pydantic models for MCP tool returns.

これらのモデルは FastMCP が JSON Schema を自動生成する際に
ツール出力の正確な構造を AI エージェントへ伝達するために使う。
全モデルで `extra="forbid"` を指定し、想定外フィールドの混入を防ぐ。

MCP ツール契約 (input/output schema 組立て、annotations、schema://tools payload
生成、FastMCP outputSchema 注入ハック) は ``mcp_contract.py`` に分離している。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class MetadataFilterDiagnostic(BaseModel):
    """0 件 + metadata_filter 指定時の per-key 診断情報 (Issue #80).

    `total==0` かつ `metadata_filter` が指定されている場合のみ
    `SearchResponse.metadata_filter_diagnostics` として返る。エージェントが
    「キーは存在するが値が全件不一致」なのか「そもそもキーが無い」のかを
    区別できるよう、観測された値サンプルとキー存在フラグを添える。
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="フィルタに使われた frontmatter キー")
    key_present_in_index: bool = Field(
        description=(
            "このキーを持つノートが index 内に 1 件以上あるか。"
            "false のときは typo の可能性が高い (ただし通常ルートでは unknown key は "
            "UNKNOWN_FRONTMATTER_KEY でより早期に拒否される — 本フラグは防衛的な冗長化)。"
        ),
    )
    observed_values_sample: list[str] = Field(
        default_factory=list,
        description=(
            "このキーで実際に観測されている値のサンプル (頻度降順、最大 5 件)。"
            "正規化済みの文字列表現。配列型 frontmatter は JSON 配列表現で入る "
            "(FrontmatterKeyInfo.sample_values と同じ)。"
            "ここに含まれない値を filter に指定していた場合、typo または存在しない値。"
        ),
    )


class SearchResponse(BaseModel):
    """`vault_search` ツールのレスポンス."""

    model_config = ConfigDict(extra="forbid")

    tier: Literal[0, 1, 2] = Field(
        description=(
            "ヒットしたキャッシュ段。0=完全一致キャッシュ, 1=ファジーキャッシュ, 2=FTS5 検索。"
            "tier=1 の total は類似クエリのキャッシュ値を再利用した近似値で、"
            "現クエリの正確な件数ではない"
        ),
    )
    total: int = Field(
        description=(
            "フィルタ後の総件数 (limit/offset 適用前)。"
            "tier=0 (完全一致) / tier=2 (FTS5) では内部結果上限での truncate なしに正確な件数。"
            "tier=1 (fuzzy cache hit) のみ類似クエリの件数を近似値として返す点に注意"
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "総件数が内部結果上限を超え、results 配列に全件が収まっていない状態 (Issue #17)。"
            "true のとき、上限 (現在 500) 以上の offset でページングを継続しても "
            "空配列しか返らない — 到達不能な領域がある。"
            "リカバリ: query をより具体的にするか tags/folder/metadata_filter で絞り込み、"
            "結果を上限以下に収めてからページングすること。"
        ),
    )
    results: list[SearchHit] = Field(
        default_factory=list,
        description="limit/offset でスライスされた検索ヒット一覧",
    )
    metadata_filter_diagnostics: list[MetadataFilterDiagnostic] | None = Field(
        default=None,
        description=(
            "Issue #80: total==0 かつ metadata_filter 指定時のみ付与される per-key 診断。"
            "各要素は filter に使われたキーの存在可否と観測値サンプルを示し、"
            "エージェントが「値が全件不一致」と「キー欠落」を区別できるようにする。"
            "それ以外の場合 (ヒットあり or filter 無し) は null。"
        ),
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
