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

from vault_search.exceptions import (
    ErrorCode,
    NoteNotFoundError,
    ValidationError,
    VaultSearchError,
)


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


class TestErrorCatalog:
    """``ERROR_CATALOG`` は error_code → metadata の単一 source of truth (#199/#200/#201).

    ``resources.py`` の ``_ERRORS`` がローカルに保持していた
    (``description`` / ``example`` / ``raised_by``) 情報を ``exceptions.py``
    に集約する。resources.py は本 CATALOG を参照して wire 形式に serialize
    するだけで、自身では例外階層を import しない (依存方向を正す #201)。

    Red フェーズ: 以下はまだ実装されていない。CATALOG 追加前に FAIL する。
    """

    def test_error_catalog_is_public_export(self) -> None:
        """ERROR_CATALOG が exceptions module の __all__ に含まれる."""
        from vault_search import exceptions

        assert "ERROR_CATALOG" in exceptions.__all__
        assert hasattr(exceptions, "ERROR_CATALOG")

    def test_error_catalog_entries_have_required_fields(self) -> None:
        """各 entry は exception_class / description / example を必ず持つ."""
        from vault_search.exceptions import ERROR_CATALOG, VaultSearchError

        assert len(ERROR_CATALOG) > 0, "CATALOG must not be empty"
        for code, info in ERROR_CATALOG.items():
            assert "exception_class" in info, f"{code}: missing exception_class"
            assert "description" in info, f"{code}: missing description"
            assert "example" in info, f"{code}: missing example"
            assert issubclass(info["exception_class"], VaultSearchError), (
                f"{code}: exception_class must subclass VaultSearchError"
            )
            assert isinstance(info["description"], str) and info["description"].strip()
            assert isinstance(info["example"], str) and info["example"].strip()

    def test_vault_search_error_entry_is_abstract(self) -> None:
        """VAULT_SEARCH_ERROR entry は abstract=True で payload から除外される (#200)."""
        from vault_search.exceptions import ERROR_CATALOG

        assert "VAULT_SEARCH_ERROR" in ERROR_CATALOG
        entry = ERROR_CATALOG["VAULT_SEARCH_ERROR"]
        assert entry.get("abstract") is True, (
            "VAULT_SEARCH_ERROR must be marked abstract=True so build_schema_payload "
            "excludes it from the agent-facing errors section."
        )

    def test_concrete_entries_are_not_abstract(self) -> None:
        """具体 error は abstract フラグが False / 未設定."""
        from vault_search.exceptions import ERROR_CATALOG

        for code, info in ERROR_CATALOG.items():
            if code == "VAULT_SEARCH_ERROR":
                continue
            assert not info.get("abstract", False), (
                f"{code}: concrete error must not be marked abstract"
            )

    def test_error_catalog_keys_subset_of_error_code_literal(self) -> None:
        """CATALOG の key は全て ErrorCode Literal の値."""
        from vault_search.exceptions import ERROR_CATALOG, ErrorCode

        declared = set(get_args(ErrorCode))
        catalog_keys = set(ERROR_CATALOG.keys())
        extra = catalog_keys - declared
        assert not extra, f"CATALOG has codes not in ErrorCode Literal: {extra}"

    def test_error_catalog_covers_all_concrete_error_codes(self) -> None:
        """全 ErrorCode (abstract 除く) が CATALOG に含まれる.

        exclusion set は CATALOG の ``abstract=True`` entry から動的に導出する
        ため、2 個目の abstract code が追加された場合も自動追随する (hardcode
        しない)。
        """
        from vault_search.exceptions import ERROR_CATALOG, ErrorCode

        declared = set(get_args(ErrorCode))
        abstract_codes = {c for c, info in ERROR_CATALOG.items() if info.get("abstract", False)}
        concrete_codes = declared - abstract_codes
        missing = concrete_codes - set(ERROR_CATALOG.keys())
        assert not missing, f"concrete ErrorCode values missing from CATALOG: {missing}"

    def test_catalog_exception_class_error_code_in_registry(self) -> None:
        """各 entry の exception_class.error_code が ErrorCode Literal の値.

        本 test は「exception_class の class-level error_code が Literal の値」
        だけを pin する軽量 drift guard。CATALOG key と class-level error_code
        の一致は意図的に要求しない: サブコード (UNKNOWN_FRONTMATTER_KEY /
        UNSUPPORTED_RANGE_OPERATOR) は ValidationError の kwarg 上書きで emit
        されるため class-level error_code (VALIDATION_ERROR) とは一致しない。
        CATALOG key と Literal の包含関係は
        ``test_error_catalog_keys_subset_of_error_code_literal`` が検証する。
        """
        from vault_search.exceptions import ERROR_CATALOG

        registry = set(get_args(ErrorCode))
        for code, info in ERROR_CATALOG.items():
            cls = info["exception_class"]
            assert cls.error_code in registry, (
                f"{code}: exception_class.error_code={cls.error_code!r} not in ErrorCode"
            )


class TestValidationErrorLocation:
    """``ValidationError`` は ``exceptions.py`` に配置される (#201).

    validation.py に置いたままだと resources.py / filter.py 等が
    ``from .validation import ValidationError`` する必要があり、
    例外階層の定義責務が 2 module に分散する。

    Red: 現状 ValidationError は validation.py にあり、
    ``from vault_search.exceptions import ValidationError`` は ImportError。
    """

    def test_validation_error_importable_from_exceptions(self) -> None:
        """exceptions module から ValidationError を import できる."""
        from vault_search.exceptions import ValidationError as VE_from_exceptions

        assert VE_from_exceptions is ValidationError

    def test_validation_error_in_exceptions_all(self) -> None:
        """exceptions.__all__ に ValidationError が含まれる."""
        from vault_search import exceptions

        assert "ValidationError" in exceptions.__all__

    def test_validation_module_does_not_re_export_validation_error(self) -> None:
        """validation.py が ValidationError を再エクスポートしない (#201 regression).

        本 PR で ValidationError は exceptions.py に移動した。validation.py
        側が後から ``__all__`` に追加し直すと import site が silent に
        divergent になるため、validation 側で公開されないことを pin する。
        """
        from vault_search import validation as val_mod

        assert "ValidationError" not in val_mod.__all__, (
            "ValidationError must not be re-exported from validation.py; "
            f"got __all__={val_mod.__all__}"
        )
