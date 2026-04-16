"""vault-search-mcp ドメイン例外階層.

schemas.py から分離 (B1-1)。モデル定義と例外定義の責務を分ける。
"""

from __future__ import annotations

from typing import ClassVar


class VaultSearchError(Exception):
    """vault-search-mcp ドメインの基底例外."""

    error_code: ClassVar[str] = "VAULT_SEARCH_ERROR"


class NoteNotFoundError(VaultSearchError, ValueError):
    """指定された path のノートがインデックスに存在しない."""

    error_code: ClassVar[str] = "NOTE_NOT_FOUND"

    def __init__(self, path: str) -> None:
        super().__init__(f"Note not found: {path}")
        self.path = path
