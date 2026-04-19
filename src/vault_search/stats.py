"""インデックスの集計状態 / スキーマメタを表す型を集約する.

検索結果や個別ノートを表す型は schemas.py に残る。
将来的に境界が揺らぐ可能性あり (PR 1b/1c で再評価)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FrontmatterKeyInfo(BaseModel):
    """frontmatter キー 1 件のメタ情報 (Issue #20)."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        description=(
            "frontmatter のキー名。ネスト dict は dotted key (例: 'meta.author') を含む。"
            "トップレベルの親 dict キー (例: 'meta') も value_type='object' として公開される "
            "(ただし metadata_filter では filter 不可 — 葉キーを使うこと)"
        )
    )
    value_type: Literal["string", "number", "boolean", "array", "object", "mixed"] = Field(
        description=(
            "観測された値型。index 時に全スカラーが文字列に正規化されるため、"
            "boolean / number はヒューリスティック推論 ('true'/'false' 完全一致で boolean、"
            "数値 regex で number、それ以外の文字列は string)。"
            "配列値は 'array' (要素型は問わない)。親 dict キーは 'object' "
            "(filter 不可、葉 dotted key を使うこと)。"
            "複数 note で型が混在する場合は 'mixed'。"
            "日付/日時は 'string' に丸まる (正規化で型情報喪失、既知の limitation)"
        )
    )
    sample_values: list[str] = Field(
        default_factory=list,
        description=(
            "出現頻度上位最大 5 件のサンプル値 (降順、重複なし、同頻度は辞書順で安定)。"
            '配列フィールドは配列全体の JSON 文字列表現 (例: \'["a", "b"]\')。'
            "配列要素別の頻度集計は vault_tags (tags 相当) を使うこと。"
            "空文字・空白のみの値は sample_values から除外するが note_count には含める "
            "(この差分から『空が多い key』が判る)。"
            "親 dict キー (value_type='object') は sample_values が空リスト"
        ),
    )
    note_count: int = Field(description="このキーを持つ note 数 (YAML null / 欠落は除外)")


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
