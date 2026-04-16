"""vault-search-mcp ドメイン例外階層.

schemas.py から分離 (B1-1)。モデル定義と例外定義の責務を分ける。
"""

from __future__ import annotations

from typing import ClassVar, Literal

__all__ = [
    "ErrorCode",
    "NoteNotFoundError",
    "VaultSearchError",
]


# ドメイン全体で有効な error_code 文字列リテラルの単一 source of truth (Issue #120).
# Literal union を使うことで:
#   - raise 側の typo を mypy が static に検知
#   - IDE autocomplete が効く
#   - 一覧を探すのに grep が不要 (本定義を読めば十分)
# 新しい code を追加する際は、ここと tests/test_exceptions.py::TestErrorCodeRegistry
# の EXPECTED 集合を同時に更新する。
ErrorCode = Literal[
    "VAULT_SEARCH_ERROR",
    "NOTE_NOT_FOUND",
    "VALIDATION_ERROR",
    "UNKNOWN_FRONTMATTER_KEY",
    "UNSUPPORTED_RANGE_OPERATOR",
]


class VaultSearchError(Exception):
    """vault-search-mcp ドメインの基底例外."""

    error_code: ClassVar[ErrorCode] = "VAULT_SEARCH_ERROR"


class NoteNotFoundError(VaultSearchError, ValueError):
    """指定された path のノートがインデックスに存在しない."""

    error_code: ClassVar[ErrorCode] = "NOTE_NOT_FOUND"

    def __init__(self, path: str) -> None:
        super().__init__(f"Note not found: {path}")
        self.path = path
