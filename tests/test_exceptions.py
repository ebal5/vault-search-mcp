"""例外階層の振る舞いを検証するテスト (Issue #18 / #32).

Red フェーズ: 以下の未実装の振る舞いが FAIL することを確認する。

- NoteNotFoundError は VaultSearchError と ValueError を多重継承する
- ValidationError は VaultSearchError と ValueError を多重継承する (#32 同時解消)
- 各例外クラスに error_code class attribute を持たせる
  - VaultSearchError.error_code == "VAULT_SEARCH_ERROR"
  - NoteNotFoundError.error_code == "NOTE_NOT_FOUND"
  - ValidationError.error_code == "VALIDATION_ERROR"
- NoteNotFoundError("x").path == "x" の既存属性は維持 (regression guard)
"""

from __future__ import annotations

import pytest

from vault_search.exceptions import NoteNotFoundError, VaultSearchError
from vault_search.validation import ValidationError


class TestVaultSearchErrorBase:
    """VaultSearchError 基底クラスの振る舞いを検証する."""

    def test_is_exception(self) -> None:
        """VaultSearchError は Exception を継承する (既存挙動)."""
        assert issubclass(VaultSearchError, Exception)

    def test_error_code_attribute(self) -> None:
        """VaultSearchError は error_code class attribute を持つ."""
        assert hasattr(VaultSearchError, "error_code")
        assert VaultSearchError.error_code == "VAULT_SEARCH_ERROR"

    def test_instance_error_code(self) -> None:
        """インスタンスからも error_code にアクセスできる."""
        err = VaultSearchError("test")
        assert err.error_code == "VAULT_SEARCH_ERROR"


class TestNoteNotFoundError:
    """NoteNotFoundError の振る舞いを検証する."""

    def test_is_vault_search_error(self) -> None:
        """NoteNotFoundError は VaultSearchError を継承する (既存挙動)."""
        assert issubclass(NoteNotFoundError, VaultSearchError)

    def test_is_value_error(self) -> None:
        """NoteNotFoundError は ValueError を継承する (Issue #18 新要件)."""
        assert issubclass(NoteNotFoundError, ValueError)

    def test_catchable_as_value_error(self) -> None:
        """NoteNotFoundError のインスタンスを ValueError で捕捉できる."""
        with pytest.raises(ValueError):
            raise NoteNotFoundError("some/note.md")

    def test_catchable_as_vault_search_error(self) -> None:
        """NoteNotFoundError のインスタンスを VaultSearchError で捕捉できる."""
        with pytest.raises(VaultSearchError):
            raise NoteNotFoundError("some/note.md")

    def test_path_attribute_preserved(self) -> None:
        """NoteNotFoundError("x").path == "x" の既存属性は維持される (regression guard)."""
        err = NoteNotFoundError("some/note.md")
        assert err.path == "some/note.md"

    def test_error_code_attribute(self) -> None:
        """NoteNotFoundError は error_code == "NOTE_NOT_FOUND" を持つ."""
        assert hasattr(NoteNotFoundError, "error_code")
        assert NoteNotFoundError.error_code == "NOTE_NOT_FOUND"

    def test_instance_error_code(self) -> None:
        """インスタンスからも error_code にアクセスできる."""
        err = NoteNotFoundError("some/note.md")
        assert err.error_code == "NOTE_NOT_FOUND"

    def test_error_code_differs_from_base(self) -> None:
        """NoteNotFoundError の error_code は VaultSearchError の値と異なる."""
        assert NoteNotFoundError.error_code != VaultSearchError.error_code


class TestValidationError:
    """ValidationError の振る舞いを検証する."""

    def test_is_value_error(self) -> None:
        """ValidationError は ValueError を継承する (既存挙動)."""
        assert issubclass(ValidationError, ValueError)

    def test_is_vault_search_error(self) -> None:
        """ValidationError は VaultSearchError を継承する (Issue #32 新要件)."""
        assert issubclass(ValidationError, VaultSearchError)

    def test_catchable_as_vault_search_error(self) -> None:
        """ValidationError のインスタンスを VaultSearchError で捕捉できる."""
        with pytest.raises(VaultSearchError):
            raise ValidationError("bad input")

    def test_catchable_as_value_error(self) -> None:
        """ValidationError のインスタンスを ValueError で捕捉できる (既存挙動維持)."""
        with pytest.raises(ValueError):
            raise ValidationError("bad input")

    def test_error_code_attribute(self) -> None:
        """ValidationError は error_code == "VALIDATION_ERROR" を持つ."""
        assert hasattr(ValidationError, "error_code")
        assert ValidationError.error_code == "VALIDATION_ERROR"

    def test_instance_error_code(self) -> None:
        """インスタンスからも error_code にアクセスできる."""
        err = ValidationError("bad input")
        assert err.error_code == "VALIDATION_ERROR"

    def test_error_code_differs_from_base(self) -> None:
        """ValidationError の error_code は VaultSearchError の値と異なる."""
        assert ValidationError.error_code != VaultSearchError.error_code
