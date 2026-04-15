"""vault-search-mcp ドメイン例外階層.

schemas.py から分離 (B1-1)。モデル定義と例外定義の責務を分ける。
"""

from __future__ import annotations


class VaultSearchError(Exception):
    """vault-search-mcp ドメインの基底例外."""


class NoteNotFoundError(VaultSearchError):
    """指定された path のノートがインデックスに存在しない."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Note not found: {path}")
        self.path = path
