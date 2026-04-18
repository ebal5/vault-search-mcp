"""VaultWatcher: watchdog イベントハンドリングのテスト.

設計方針 (#79):

- **deterministic 経路**: ``VaultEventHandler`` を直接構築し、合成 watchdog
  イベントを ``on_any_event`` に流す。Observer/filesystem を経由しないため
  inotify 登録 race / SQLite busy / FSEvents 遅延の影響を受けない。RENAME
  のシナリオ網羅はこちらで担う。
- **integration 経路**: 実 Observer + 実 rename を経由する path も最小限残し、
  end-to-end の wire (Observer → Handler → debounce → update_single → DB) と
  logging 副作用を実環境で確認する。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip("watchdog")

from watchdog.events import (  # noqa: E402
    FileCreatedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)

from vault_search.indexer import VaultIndex  # noqa: E402
from vault_search.watcher import VaultEventHandler, VaultWatcher  # noqa: E402


def _wait_until(pred, timeout: float = 3.0) -> bool:
    """pred() が True になるまで最大 timeout 秒ポーリング (integration 経路用)."""
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
# Deterministic Handler tests — Observer/filesystem を介さず合成イベントを流す
# ---------------------------------------------------------------------------


def _make_handler(vault: Path) -> tuple[VaultEventHandler, list[str]]:
    """``VaultEventHandler`` と schedule された rel_path を記録する list を返す."""
    scheduled: list[str] = []
    handler = VaultEventHandler(vault.resolve(), scheduled.append)
    return handler, scheduled


# (src_rel, dest_rel, expected_scheduled_rels)
RENAME_HANDLER_CASES = [
    pytest.param("A.md", "B.md", ["A.md", "B.md"], id="flat_rename"),
    pytest.param("sub/A.md", "sub2/A.md", ["sub/A.md", "sub2/A.md"], id="cross_subdir"),
    # hidden -> normal: src は除外、dest のみ schedule
    pytest.param(".archive/A.md", "A.md", ["A.md"], id="hidden_to_normal"),
    # cross-extension: src は非 .md で除外、dest のみ schedule
    pytest.param("A.txt", "A.md", ["A.md"], id="cross_extension_txt_to_md"),
    # normal -> hidden: src のみ schedule、dest は除外
    pytest.param("note.md", ".archive/note.md", ["note.md"], id="normal_to_hidden"),
    # normal -> _underscore: src のみ schedule、dest は除外
    pytest.param("note.md", "_drafts/note.md", ["note.md"], id="normal_to_underscore"),
    # md -> txt: src のみ schedule、dest は非 .md で除外
    pytest.param("A.md", "A.txt", ["A.md"], id="md_to_txt"),
]


@pytest.mark.parametrize("src_rel,dest_rel,expected", RENAME_HANDLER_CASES)
def test_handler_schedules_rename_paths(
    vault: Path, src_rel: str, dest_rel: str, expected: list[str]
) -> None:
    """合成 ``FileMovedEvent`` で src/dest が期待通り schedule されること.

    filesystem を経由しないため Observer 登録 race / inotify レイテンシに
    依存しない。RENAME のシナリオ網羅はこの経路で担保する (#79)。
    """
    handler, scheduled = _make_handler(vault)
    src_full = str(vault / src_rel)
    dest_full = str(vault / dest_rel)
    handler.on_any_event(FileMovedEvent(src_full, dest_full))
    assert scheduled == expected


def test_handler_schedules_on_create(vault: Path) -> None:
    """``FileCreatedEvent`` で対象 .md が schedule される."""
    handler, scheduled = _make_handler(vault)
    handler.on_any_event(FileCreatedEvent(str(vault / "fresh.md")))
    assert scheduled == ["fresh.md"]


def test_handler_schedules_on_delete(vault: Path) -> None:
    """``FileDeletedEvent`` で対象 .md が schedule される."""
    handler, scheduled = _make_handler(vault)
    handler.on_any_event(FileDeletedEvent(str(vault / "gone.md")))
    assert scheduled == ["gone.md"]


def test_handler_skips_non_md(vault: Path) -> None:
    """非 .md ファイルは schedule されない (拡張子フィルタ)."""
    handler, scheduled = _make_handler(vault)
    handler.on_any_event(FileCreatedEvent(str(vault / "notes.txt")))
    handler.on_any_event(FileCreatedEvent(str(vault / "image.png")))
    assert scheduled == []


def test_handler_skips_path_outside_vault(vault: Path, tmp_path: Path) -> None:
    """vault_root 外の .md は schedule されない (relative_to 失敗)."""
    handler, scheduled = _make_handler(vault)
    outside = tmp_path / "elsewhere" / "x.md"
    handler.on_any_event(FileCreatedEvent(str(outside)))
    assert scheduled == []


def test_handler_skips_directory_events(vault: Path) -> None:
    """ディレクトリイベントは schedule されない (is_directory=True)."""
    handler, scheduled = _make_handler(vault)
    event = FileCreatedEvent(str(vault / "sub"))
    event.is_directory = True
    handler.on_any_event(event)
    assert scheduled == []


def test_handler_logs_rename_at_info(vault: Path, caplog: pytest.LogCaptureFixture) -> None:
    """``FileMovedEvent`` で ``rename detected`` の INFO log が出る (#78)."""
    handler, _ = _make_handler(vault)
    with caplog.at_level(logging.INFO, logger="vault_search.watcher"):
        handler.on_any_event(FileMovedEvent(str(vault / "A.md"), str(vault / "B.md")))
    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("rename detected" in m for m in info_msgs), info_msgs


def test_handler_logs_debug_on_hidden_dest_exclusion(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """隠しフォルダ宛 rename で dest 除外時に DEBUG log が出る (#78)."""
    handler, _ = _make_handler(vault)
    with caplog.at_level(logging.DEBUG, logger="vault_search.watcher"):
        handler.on_any_event(
            FileMovedEvent(str(vault / "note.md"), str(vault / ".archive" / "note.md"))
        )
    debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(".archive" in m or "excluded" in m or "skip" in m for m in debug_msgs), debug_msgs


# ---------------------------------------------------------------------------
# Integration smoke tests — 実 Observer + 実 filesystem の wire を担保
# ---------------------------------------------------------------------------


def test_watcher_indexes_on_create_and_delete(vault: Path, index: VaultIndex) -> None:
    """create/delete イベントが Observer → Handler → DB まで wire されること.

    deterministic 経路で個別ロジックは網羅済 (test_handler_*)。ここでは
    end-to-end で実 Observer + sqlite commit の整合性のみ確認する。
    """
    index.build_index()
    note = vault / "note.md"

    with _running_watcher(index):
        note.write_text("# N\nbody\n", encoding="utf-8")
        assert _wait_until(lambda: index.note_exists("note.md"))
        note.unlink()
        assert _wait_until(lambda: not index.note_exists("note.md"))


def test_watcher_indexes_rename_end_to_end(vault: Path, index: VaultIndex) -> None:
    """実 rename イベントが Observer → Handler → debounce → DB まで wire されること.

    deterministic 経路で 7 シナリオを網羅 (test_handler_schedules_rename_paths)
    しているので、ここは smoke 1 件で wire 健全性のみ確認する。
    """
    src = vault / "A.md"
    src.write_text("# A\nbody\n", encoding="utf-8")
    index.build_index()
    dest = vault / "B.md"

    with _running_watcher(index):
        src.rename(dest)
        assert _wait_until(lambda: index.note_exists("B.md"))


def test_watcher_logs_indexed_at_info_on_flush(
    vault: Path, index: VaultIndex, caplog: pytest.LogCaptureFixture
) -> None:
    """``_flush`` 成功後に ``indexed:`` INFO log が出る (#78).

    ``_flush`` は VaultWatcher 側のメソッドのため、deterministic 経路
    (Handler 単体) では検証できない。実 Observer 経路で確認する。
    """
    index.build_index()

    with caplog.at_level(logging.INFO, logger="vault_search.watcher"):
        with _running_watcher(index):
            (vault / "new_note.md").write_text("# New\nbody\n", encoding="utf-8")
            _wait_until(lambda: index.note_exists("new_note.md"))

    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("indexed:" in m for m in info_msgs), info_msgs
