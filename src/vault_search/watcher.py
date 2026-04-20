"""watchdog ベースの Vault ファイル監視.

``VaultIndex`` の前段として ``.md`` ファイルの作成/変更/削除/移動を監視し、
debounce 付きで ``update_single`` を呼び出してインデックスを差分更新する。
watchdog が import できない環境では polling フォールバック (start() が False 返却)。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .indexer import VaultIndex

logger = logging.getLogger(__name__)


try:
    from watchdog.events import (
        FileSystemEvent,
        FileSystemEventHandler,
    )
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - watchdog is a hard dep but keep guard for sdist installs
    # `watchdog.events` と `watchdog.observers` のどちらが欠けても利用不可扱い (#172)。
    # 旧実装は events のみ check し、Observer は start() 内で lazy import していたため
    # 部分的 sdist インストールで `_WATCHDOG_AVAILABLE=True` でも start() が
    # ImportError を投げ、main() の graceful fallback (return False) を迂回した。
    _WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]


class VaultEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """watchdog FileSystemEventHandler を vault 専用フィルタ + callback に閉じ込める.

    Observer / filesystem を介さず、テストから直接各 handler メソッドに合成
    イベントを流せるよう module-level に切り出している (#79)。

    Dispatch 構造 (#77): watchdog 慣例に合わせて ``on_created`` / ``on_modified``
    / ``on_deleted`` / ``on_moved`` を個別 override する。汎用 ``on_any_event``
    フォールバックは使わないことで:

    - ``isinstance(event, FileMovedEvent)`` 分岐を排除
    - 将来 watchdog に追加される ``FileClosedEvent`` 等による意図しない
      再インデックスを避ける
    - ``on_moved`` を他所から差し替えた際の src/dest 二重 schedule を構造的に防ぐ

    パス判定は ``VaultIndex.is_indexable_path`` に委譲する (#76)。``.md`` 拡張子
    / ``vault_root`` 配下 / 除外プレフィックスの判定が walker (`_iter_markdown_files`)
    と単一ソース化される。
    """

    def __init__(self, index: VaultIndex, schedule_callback: Callable[[str], None]) -> None:
        self._index = index
        self._schedule = schedule_callback

    def _schedule_if_valid(self, raw_path: str) -> None:
        rel = self._index.is_indexable_path(raw_path)
        if rel is None:
            logger.debug("skipped non-indexable path: %s", raw_path)
            return
        self._schedule(rel)

    def on_created(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._schedule_if_valid(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._schedule_if_valid(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._schedule_if_valid(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        # FileMovedEvent はリネーム/移動。src_path (旧) と dest_path (新)
        # の両方をインデックス更新対象にする (#58)。
        logger.info(
            "rename detected: %s -> %s",
            event.src_path,
            event.dest_path,
        )
        self._schedule_if_valid(event.src_path)
        self._schedule_if_valid(event.dest_path)


class VaultWatcher:
    """watchdog ベースのファイル監視.

    watchdog が無い環境では polling フォールバック。
    """

    def __init__(self, index: VaultIndex, debounce_sec: float = 2.0):
        self._index = index
        self._debounce_sec = debounce_sec
        self._observer: Any = None
        self._pending: dict[str, float] = {}
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._watcher_failure_count: int = 0
        self._last_watcher_error_at: str | None = None

    def start(self) -> bool:
        """監視開始。watchdog が利用可能なら True."""
        if not _WATCHDOG_AVAILABLE:
            logger.warning("watchdog not installed — file watching disabled")
            return False

        handler = VaultEventHandler(self._index, self._schedule_update)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._index.vault_root), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("File watcher started: %s", self._index.vault_root)
        return True

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            if self._observer.is_alive():
                self._observer.join(timeout=5)
            self._observer = None

    def _schedule_update(self, rel_path: str) -> None:
        """デバウンス付きインデックス更新スケジューリング."""
        with self._lock:
            self._pending[rel_path] = time.monotonic()

            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_sec, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def failure_stats(self) -> dict[str, Any]:
        """差分更新の失敗累計を返す (#39).

        agent が ``vault_reindex`` 経由で watcher の健全性を把握できるよう、
        起動以降の _flush 失敗カウントと最新失敗時刻 (UTC) を公開する。
        """
        with self._lock:
            return {
                "watcher_failure_count": self._watcher_failure_count,
                "last_watcher_error_at": self._last_watcher_error_at,
            }

    def _flush(self) -> None:
        with self._lock:
            paths = list(self._pending.keys())
            self._pending.clear()

        n = len(paths)
        for i, rel_path in enumerate(paths):
            try:
                self._index.update_single(rel_path)
                logger.info("indexed: %s (pending=%d)", rel_path, n - i - 1)
            except Exception:
                logger.exception("Failed to update index for %s", rel_path)
                with self._lock:
                    self._watcher_failure_count += 1
                    self._last_watcher_error_at = datetime.now(timezone.utc).isoformat()
