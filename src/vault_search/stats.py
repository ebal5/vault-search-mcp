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
            "(ただし metadata_filter では not filterable — 葉 dotted key を使うこと。"
            "親キー名を metadata_filter に渡すと UNKNOWN_FRONTMATTER_KEY が返る)"
        )
    )
    value_type: Literal["string", "number", "boolean", "array", "object", "mixed"] = Field(
        description=(
            "観測された値型。index 時に全スカラーが文字列に正規化されるため、"
            "boolean / number はヒューリスティック推論 ('true'/'false' 完全一致で boolean、"
            "数値 regex (指数表記含む) で number、それ以外の文字列は string)。"
            "配列値は 'array' (要素型は問わない)。親 dict キーは 'object' "
            "(not filterable、葉 dotted key を使うこと)。"
            "複数 note で型が混在する場合は 'mixed' — filter は可能で、値は常に文字列表現 "
            "(例: metadata_filter={'level': '5'} で int 5 の note もマッチ)。"
            "日付/日時は 'string' に丸まる (正規化で型情報喪失、既知の limitation)。"
            "YAML で引用符付きの数値文字列 (例: code: '007') も 'number' に分類される"
            " (heuristic の限界)"
        )
    )
    sample_values: list[str] = Field(
        default_factory=list,
        description=(
            "出現頻度上位最大 5 件のサンプル値 (降順、重複なし、同頻度は辞書順で安定)。"
            "これらは正規化済みの文字列表現で、そのまま metadata_filter の "
            "eq / ne / in 値として使える (例: date は isoformat、int 5 は '5' として格納)。"
            '配列フィールドは配列全体の JSON 文字列表現 (例: \'["a", "b"]\')。'
            "この文字列をそのまま eq に渡すと配列全体一致の filter になるが通常は意図しない — "
            "array 要素で filter したい場合は要素値 (例: 'a') を渡すこと。"
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
