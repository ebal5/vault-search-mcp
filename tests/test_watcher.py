"""VaultWatcher: watchdog イベントハンドリングのテスト."""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip("watchdog")

from vault_search.indexer import VaultIndex  # noqa: E402
from vault_search.watcher import VaultWatcher  # noqa: E402


def _indexed(index: VaultIndex, rel_path: str) -> bool:
    """rel_path が index に入っているかを DB 直叩きで確認."""
    conn = index._connect()
    try:
        row = conn.execute("SELECT 1 FROM notes WHERE path = ?", (rel_path,)).fetchone()
    finally:
        conn.close()
    return row is not None


def _wait_until(pred, timeout: float = 3.0) -> bool:
    """pred() が True になるまで最大 timeout 秒ポーリング."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


@contextmanager
def _running_watcher(index: VaultIndex):
    """VaultWatcher を起動し、Observer スレッドの watch 登録を短く待つ."""
    watcher = VaultWatcher(index, debounce_sec=0.1)
    assert watcher.start()
    # Observer スレッドが recursive watch を登録し終える前に rename されると
    # FileMovedEvent を取りこぼす可能性があるため、軽いバリアを入れる。
    time.sleep(0.2)
    try:
        yield watcher
    finally:
        watcher.stop()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.fixture
def index(vault: Path, tmp_path: Path) -> VaultIndex:
    return VaultIndex(vault, db_path=tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Rename — FileMovedEvent の dest_path が index に反映されること (#58)
# ---------------------------------------------------------------------------


RENAME_CASES = [
    # flat: src/dest 共に md、vault 直下
    pytest.param("A.md", "B.md", "B.md", id="flat_rename"),
    # cross-subdir: サブディレクトリを跨ぐ移動
    pytest.param("sub/A.md", "sub2/A.md", "sub2/A.md", id="cross_subdir"),
    # hidden -> normal: 除外フォルダから出てきた md が index に入る
    # (src は元々 index されていない想定)
    pytest.param(".archive/A.md", "A.md", "A.md", id="hidden_to_normal"),
    # cross-extension: 非 md → md で dest 側だけ index される
    pytest.param("A.txt", "A.md", "A.md", id="cross_extension_txt_to_md"),
]


@pytest.mark.parametrize("src_rel,dest_rel,expect_indexed", RENAME_CASES)
def test_watcher_indexes_rename_dest_path(
    vault: Path, index: VaultIndex, src_rel: str, dest_rel: str, expect_indexed: str
) -> None:
    """FileMovedEvent の dest_path が `_schedule_update` に流れること (#58).

    旧実装は ``event.src_path`` しか見ておらず、以下のケースで dest が
    未インデックス化されていた:

    - ``flat_rename`` / ``cross_subdir``: src が削除されるだけで
      新パスが次の modify event まで index に入らない
    - ``hidden_to_normal``: src が隠しフォルダで早期 return し、
      dest が処理されない
    - ``cross_extension_txt_to_md``: src が非 md で早期 return し、
      dest が処理されない
    """
    src = vault / src_rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Note\nhello unique_marker_xyz\n", encoding="utf-8")
    index.build_index()

    dest = vault / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    with _running_watcher(index):
        src.rename(dest)
        assert _wait_until(lambda: _indexed(index, expect_indexed)), (
            f"{expect_indexed} should be indexed after rename {src_rel} -> {dest_rel}"
        )


def test_watcher_excludes_hidden_dest(vault: Path, index: VaultIndex) -> None:
    """通常→隠しフォルダへの移動で dest は index されず src は index から消える.

    隠しフォルダフィルタが src / dest 両方に適用されることの regression guard。
    """
    src = vault / "note.md"
    src.write_text("# N\nhello\n", encoding="utf-8")
    index.build_index()
    assert _indexed(index, "note.md")

    hidden_dir = vault / ".archive"
    hidden_dir.mkdir()
    dest = hidden_dir / "note.md"

    with _running_watcher(index):
        src.rename(dest)
        # 旧パスは index から消える (src 側の _schedule_update 経由で DELETE)
        assert _wait_until(lambda: not _indexed(index, "note.md"))
        # 新パス (.archive/note.md) は隠しフォルダ除外で index されない
        time.sleep(0.3)  # debounce + α を待っても入らないことを確認
        assert not _indexed(index, ".archive/note.md")


# ---------------------------------------------------------------------------
# Create / Delete — Handler リファクタに伴う非-FileMovedEvent 経路の regression guard
# ---------------------------------------------------------------------------


def test_watcher_indexes_on_create(vault: Path, index: VaultIndex) -> None:
    """新規 .md ファイル作成で index に入る (FileCreatedEvent 経路)."""
    index.build_index()  # 空 vault を初期化
    with _running_watcher(index):
        note = vault / "fresh.md"
        note.write_text("# Fresh\nbody\n", encoding="utf-8")
        assert _wait_until(lambda: _indexed(index, "fresh.md"))


def test_watcher_removes_on_delete(vault: Path, index: VaultIndex) -> None:
    """既存 .md ファイル削除で index から消える (FileDeletedEvent 経路)."""
    note = vault / "gone.md"
    note.write_text("# Gone\nbody\n", encoding="utf-8")
    index.build_index()
    assert _indexed(index, "gone.md")

    with _running_watcher(index):
        note.unlink()
        assert _wait_until(lambda: not _indexed(index, "gone.md"))
