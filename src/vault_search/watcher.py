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
from pathlib import Path
from typing import Any

from .indexer import VaultIndex

logger = logging.getLogger(__name__)


try:
    from watchdog.events import (
        FileMovedEvent,
        FileSystemEvent,
        FileSystemEventHandler,
    )

    _WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - watchdog is a hard dep but keep guard for sdist installs
    _WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore[assignment,misc]


class VaultEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """watchdog FileSystemEventHandler を vault 専用フィルタ + callback に閉じ込める.

    Observer / filesystem を介さず、テストから直接 ``on_any_event`` に合成
    イベントを流せるよう module-level に切り出している (#79)。
    """

    def __init__(self, vault_root: Path, schedule_callback: Callable[[str], None]) -> None:
        self._vault_root = vault_root
        self._schedule = schedule_callback

    def _schedule_if_valid(self, raw_path: str) -> None:
        if not raw_path.endswith(".md"):
            logger.debug("skipped non-.md path: %s", raw_path)
            return
        try:
            rel = str(Path(raw_path).relative_to(self._vault_root)).replace("\\", "/")
        except ValueError:
            logger.debug("skipped path outside vault: %s", raw_path)
            return
        if any(p.startswith(".") or p.startswith("_") for p in Path(rel).parts):
            logger.debug("skipped excluded path: %s", rel)
            return
        self._schedule(rel)

    def on_any_event(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        # FileMovedEvent はリネーム/移動。src_path (旧) と
        # dest_path (新) の両方をインデックス更新対象にする (#58)。
        if isinstance(event, FileMovedEvent):
            logger.info(
                "rename detected: %s -> %s",
                event.src_path,
                event.dest_path,
            )
            self._schedule_if_valid(event.src_path)
            self._schedule_if_valid(event.dest_path)
            return
        self._schedule_if_valid(event.src_path)


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

    def start(self) -> bool:
        """監視開始。watchdog が利用可能なら True."""
        if not _WATCHDOG_AVAILABLE:
            logger.warning("watchdog not installed — file watching disabled")
            return False

        from watchdog.observers import Observer

        handler = VaultEventHandler(self._index.vault_root, self._schedule_update)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._index.vault_root), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("File watcher started: %s", self._index.vault_root)
        return True

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
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
