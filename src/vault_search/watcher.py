"""watchdog ベースの Vault ファイル監視.

``VaultIndex`` の前段として ``.md`` ファイルの作成/変更/削除/移動を監視し、
debounce 付きで ``update_single`` を呼び出してインデックスを差分更新する。
watchdog が import できない環境では polling フォールバック (start() が False 返却)。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .indexer import VaultIndex

logger = logging.getLogger(__name__)


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
        try:
            from watchdog.events import (
                FileMovedEvent,
                FileSystemEvent,
                FileSystemEventHandler,
            )
            from watchdog.observers import Observer

            watcher = self

            def _schedule_if_valid(raw_path: str) -> None:
                if not raw_path.endswith(".md"):
                    logger.debug("skipped non-.md path: %s", raw_path)
                    return
                try:
                    rel = str(Path(raw_path).relative_to(watcher._index.vault_root)).replace(
                        "\\", "/"
                    )
                except ValueError:
                    logger.debug("skipped path outside vault: %s", raw_path)
                    return
                if any(p.startswith(".") or p.startswith("_") for p in Path(rel).parts):
                    logger.debug("skipped excluded path: %s", rel)
                    return
                watcher._schedule_update(rel)

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event: FileSystemEvent) -> None:
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
                        _schedule_if_valid(event.src_path)
                        _schedule_if_valid(event.dest_path)
                        return
                    _schedule_if_valid(event.src_path)

            self._observer = Observer()
            self._observer.schedule(Handler(), str(self._index.vault_root), recursive=True)
            self._observer.daemon = True
            self._observer.start()
            logger.info("File watcher started: %s", self._index.vault_root)
            return True

        except ImportError:
            logger.warning("watchdog not installed — file watching disabled")
            return False

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
