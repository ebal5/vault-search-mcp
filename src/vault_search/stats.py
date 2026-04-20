"""インデックス処理の集計結果を表す時系列の統計型を集約する.

vault の静的なスキーマ記述 (FrontmatterKeyInfo 等) は `schema_meta.py` に、
検索結果や個別ノートを表す型は `schemas.py` に分離されている。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ReindexStats(BaseModel):
    """`vault_reindex` ツールのレスポンス: 処理件数の内訳."""

    model_config = ConfigDict(extra="forbid")

    added: int = Field(description="新規追加されたノート数")
    updated: int = Field(description="更新されたノート数 (mtime 進行)")
    deleted: int = Field(description="削除されたノート数 (ファイル消失)")
    skipped: int = Field(description="mtime 変化なしでスキップされたノート数")
    errors: int = Field(description="パース失敗したノート数")
    watcher_failure_count: int = Field(
        default=0,
        description=(
            "VaultWatcher が差分更新で失敗した累計件数 (プロセス起動以降)。"
            "0 より大きいとき、watcher が監視している Vault の一部が"
            "インデックスと drift している可能性がある。"
            "--no-watch で watcher 無効の場合と、一度も失敗していない場合はいずれも 0。"
            "リカバリ: `vault_reindex(force=True)` を呼ぶことでインデックスを"
            "完全再構築できる"
        ),
    )
    last_watcher_error_at: str | None = Field(
        default=None,
        description=(
            "VaultWatcher 最新の失敗時刻 (UTC, isoformat '+00:00' 形式。"
            "例: '2026-04-19T12:00:00+00:00')。"
            "一度も失敗していない / watcher 無効の場合は null。"
            "watcher_failure_count が 0 でない場合の最新エラーのみ指す"
        ),
    )
    watcher_active: bool = Field(
        default=False,
        description=(
            "VaultWatcher が起動中であれば true。"
            "--no-watch オプション指定時または watchdog 未インストール時は false。"
            "true でも watcher_failure_count が 0 より大きければ drift の可能性がある。"
            "watcher_failure_count=0 のみでは「未起動」と「失敗ゼロ」を区別できないため、"
            "watcher 有効性の判定にはこのフィールドを参照する"
        ),
    )


class VaultStats(BaseModel):
    """`vault_stats` ツールのレスポンス: インデックス全体の統計."""

    model_config = ConfigDict(extra="forbid")

    total_notes: int = Field(description="インデックス済みノート総数")
    db_size_bytes: int = Field(description="SQLite DB ファイルのサイズ (バイト)")
    db_size_mb: float = Field(description="SQLite DB ファイルのサイズ (MB, 小数2桁)")
    vault_root: str = Field(description="Vault ルートの絶対パス")
