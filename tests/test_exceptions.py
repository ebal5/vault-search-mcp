"""例外階層の振る舞いを検証するテスト (Issue #18 / #32 / #19).

Red フェーズ: 以下の未実装の振る舞いが FAIL することを確認する。

- NoteNotFoundError は VaultSearchError と ValueError を多重継承する
- ValidationError は VaultSearchError と ValueError を多重継承する (#32 同時解消)
- 各例外クラスに error_code class attribute を持たせる
  - VaultSearchError.error_code == "VAULT_SEARCH_ERROR"
  - NoteNotFoundError.error_code == "NOTE_NOT_FOUND"
  - ValidationError.error_code == "VALIDATION_ERROR"
- NoteNotFoundError("x").path == "x" の既存属性は維持 (regression guard)
- ValidationError の structured 属性: error_code 上書き / hint / did_you_mean / allowed (#19)
"""

from __future__ import annotations

from typing import get_args

import pytest

from vault_search.exceptions import ErrorCode, NoteNotFoundError, VaultSearchError
from vault_search.validation import ValidationError


class TestErrorCodeRegistry:
    """``ErrorCode`` Literal はドメイン全体で有効な error_code 値を一覧化する (Issue #120).

    Literal の ``get_args`` で取り出せる要素集合が ``exceptions.py`` /
    ``validation.py`` / ``filter.py`` で raise されるすべての error_code
    文字列と一致することを pin する。新しい code を追加する際は、Literal
    メンバと本テストを同時に更新する。
    """

    EXPECTED = {
        "VAULT_SEARCH_ERROR",
        "NOTE_NOT_FOUND",
        "VALIDATION_ERROR",
        "UNKNOWN_FRONTMATTER_KEY",
        "UNSUPPORTED_RANGE_OPERATOR",
    }

    def test_error_code_members_pinned(self) -> None:
        assert set(get_args(ErrorCode)) == self.EXPECTED

    def test_exception_classvar_defaults_in_registry(self) -> None:
        """Class-level default error_code も Literal に含まれる."""
        assert VaultSearchError.error_code in get_args(ErrorCode)
        assert NoteNotFoundError.error_code in get_args(ErrorCode)
        assert ValidationError.error_code in get_args(ErrorCode)


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


class TestValidationErrorStructured:
    """ValidationError の structured 属性を検証する (Issue #19).

    Red フェーズ: 以下の未実装の振る舞いが FAIL することを確認する。
    - error_code keyword arg でインスタンス側の error_code を上書きできる
    - hint / did_you_mean / allowed keyword args を受け取れる
    - デフォルト (kw なし) のとき各属性は適切な空値
    - 従来の positional arg 呼出しが regression しない
    """

    def test_error_code_kwarg_overrides_class_attr(self) -> None:
        """error_code keyword arg でインスタンス側の error_code が上書きされる."""
        err = ValidationError("msg", error_code="UNKNOWN_FRONTMATTER_KEY")
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"

    def test_hint_kwarg(self) -> None:
        """hint keyword arg が err.hint として参照できる."""
        err = ValidationError("msg", hint="see schema://tools")
        assert err.hint == "see schema://tools"

    def test_did_you_mean_kwarg(self) -> None:
        """did_you_mean keyword arg が err.did_you_mean として tuple で参照できる."""
        err = ValidationError("msg", did_you_mean=["priority"])
        assert err.did_you_mean == ("priority",)

    def test_allowed_kwarg(self) -> None:
        """allowed keyword arg が err.allowed として tuple で参照できる."""
        err = ValidationError("msg", allowed=["a", "b"])
        assert err.allowed == ("a", "b")

    def test_default_hint_is_none(self) -> None:
        """デフォルト (kw なし) のとき err.hint は None."""
        err = ValidationError("msg")
        assert err.hint is None

    def test_default_did_you_mean_is_empty_sequence(self) -> None:
        """デフォルト (kw なし) のとき err.did_you_mean は空 sequence."""
        err = ValidationError("msg")
        assert len(err.did_you_mean) == 0

    def test_default_allowed_is_empty_sequence(self) -> None:
        """デフォルト (kw なし) のとき err.allowed は空 sequence."""
        err = ValidationError("msg")
        assert len(err.allowed) == 0

    def test_positional_arg_regression(self) -> None:
        """ValidationError("msg") の従来通りの positional arg 呼出しが regression しない."""
        err = ValidationError("bad input")
        assert str(err) == "bad input"

    def test_str_contains_message(self) -> None:
        """str(err) には message が含まれる (従来挙動維持)."""
        err = ValidationError("something went wrong")
        assert "something went wrong" in str(err)
