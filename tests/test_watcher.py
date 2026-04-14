"""VaultWatcher: watchdog イベントハンドリングのテスト."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from vault_search.indexer import VaultIndex, VaultWatcher


def _wait_indexed(index: VaultIndex, rel_path: str, timeout: float = 3.0) -> bool:
    """rel_path が index に入るまで最大 timeout 秒待つ."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = index._connect()
        try:
            row = conn.execute("SELECT 1 FROM notes WHERE path = ?", (rel_path,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            return True
        time.sleep(0.05)
    return False


def _wait_removed(index: VaultIndex, rel_path: str, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = index._connect()
        try:
            row = conn.execute("SELECT 1 FROM notes WHERE path = ?", (rel_path,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def test_watcher_reindexes_on_rename(empty_vault: Path, tmp_path: Path) -> None:
    """ノートをリネームすると旧パスが消え、新パスがインデックスに入る (#58).

    watchdog の FileMovedEvent は src_path (旧) / dest_path (新) の両方を持つ。
    旧実装は src_path しか見ず、新パスが次の modify まで未インデックス化される
    データ欠落バグがあった。
    """
    pytest.importorskip("watchdog")

    note_a = empty_vault / "A.md"
    note_a.write_text("# A\nhello unique_marker_xyz\n", encoding="utf-8")

    db_path = tmp_path / "test.db"
    index = VaultIndex(empty_vault, db_path=db_path)
    index.build_index()

    watcher = VaultWatcher(index, debounce_sec=0.1)
    assert watcher.start()
    try:
        note_b = empty_vault / "B.md"
        note_a.rename(note_b)

        # 旧パスはインデックスから消え、新パスが入ることを期待
        assert _wait_removed(index, "A.md"), "A.md should be removed from index"
        assert _wait_indexed(index, "B.md"), "B.md should be indexed after rename"
    finally:
        watcher.stop()
