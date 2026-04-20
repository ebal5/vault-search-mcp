"""vault-search-mcp ドメイン例外階層と error catalog.

schemas.py から分離 (B1-1)。モデル定義と例外定義の責務を分ける。

``ValidationError`` と ``ERROR_CATALOG`` も本 module に集約する (#201)。
以前は ``validation.py`` に ValidationError が置かれ、``resources.py`` が
``_ERRORS`` dict を独自に保持していたため、例外階層の記述が 3 module に
分散していた。本 module を単一 source of truth とすることで:

* resources.py は例外階層そのものを import せず ``ERROR_CATALOG`` だけを読む
  (依存方向を下層 → 上層へ統一)
* 新規 ErrorCode 追加時は本 module 1 箇所の更新で済む
* 例外クラスと description / example が隣接するため drift を目視で検知しやすい

``ERROR_CATALOG`` の value には live ``exception_class`` 参照を持たせる — wire
形式への serialize は ``resources.py`` の ``build_schema_payload`` 側で行い
(class → ``__name__``)、本 module は serialization しない。CATALOG の
``exception_class`` フィールドは internal (JSON serializable ではない)。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import ClassVar, Literal, TypedDict

# ``typing.NotRequired`` is Python 3.11+; fall back to ``typing_extensions`` on 3.10
# (already available as a transitive dependency via pydantic / mcp).
if sys.version_info >= (3, 11):
    from typing import NotRequired
else:  # pragma: no cover
    from typing_extensions import NotRequired

__all__ = [
    "ERROR_CATALOG",
    "ErrorCode",
    "ExceptionInfo",
    "NoteNotFoundError",
    "ValidationError",
    "VaultSearchError",
]


# ドメイン全体で有効な error_code 文字列リテラルの単一 source of truth (Issue #120).
# Literal union を使うことで:
#   - raise 側の typo を mypy が static に検知
#   - IDE autocomplete が効く
#   - 一覧を探すのに grep が不要 (本定義を読めば十分)
# 新しい code を追加する際は、ここと ERROR_CATALOG / tests/test_exceptions.py::
# TestErrorCodeRegistry の EXPECTED 集合を同時に更新する。
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


class ValidationError(VaultSearchError, ValueError):
    """エージェント入力の検証失敗 (識別子 / ページング / filter 等).

    ``VaultSearchError`` と ``ValueError`` を多重継承。``except ValueError``
    が通る既存コードを壊さずに、``except ValidationError`` で validation 固有
    の捕捉も可能にする。

    Parameters
    ----------
    message:
        Human-readable description of the validation failure.
    error_code:
        Per-instance error code; defaults to ``"VALIDATION_ERROR"``. サブコード
        (``"UNKNOWN_FRONTMATTER_KEY"`` / ``"UNSUPPORTED_RANGE_OPERATOR"``) を
        kwarg で渡すと class-level の ``error_code`` を上書きできる。
    hint:
        Optional short guidance for self-correction (e.g. "see schema://tools").
    did_you_mean:
        Optional list of close-match candidates (from difflib).
    allowed:
        Optional sorted list of all allowed values / keys.
    unknown_keys:
        Optional per-key close-match map for batched unknown-key reports.
    """

    error_code: ErrorCode = "VALIDATION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode = "VALIDATION_ERROR",
        hint: str | None = None,
        did_you_mean: Sequence[str] | None = None,
        allowed: Sequence[str] | None = None,
        unknown_keys: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint
        self.did_you_mean: tuple[str, ...] = tuple(did_you_mean) if did_you_mean else ()
        self.allowed: tuple[str, ...] = tuple(allowed) if allowed else ()
        self.unknown_keys: dict[str, tuple[str, ...]] = (
            {k: tuple(v) for k, v in unknown_keys.items()} if unknown_keys else {}
        )


class ExceptionInfo(TypedDict):
    """``ERROR_CATALOG`` の value type.

    ``exception_class`` は live class 参照で、``raised_by`` の drift guard として
    機能する。JSON serializable ではないため wire payload に入れる際は
    ``resources._serialize_error_catalog()`` が ``exception_class.__name__`` に
    展開する (``build_schema_payload`` が内部で呼出す)。

    .. warning::
       ``ERROR_CATALOG`` を ``json.dumps`` に直接渡すと ``exception_class`` が
       ``type`` オブジェクトのため ``TypeError: Object of type type is not JSON
       serializable`` になる。wire 形式が必要な場合は
       ``resources._serialize_error_catalog()`` を経由すること。

    ``abstract`` が ``True`` の entry は agent-facing payload (``schema://tools``
    の ``errors`` セクション) から除外される。``VaultSearchError`` のような
    基底例外が該当し、agent が pattern-match する対象ではないことを示す (#200)。
    ``abstract`` フラグは class 属性ではなく CATALOG metadata として管理する —
    serialize 時の判定は ``_serialize_error_catalog()`` が CATALOG の本フラグを
    参照するため、class 側に ``abstract = True`` を追加しても二重管理になる
    だけで動作には影響しない。
    """

    exception_class: type[VaultSearchError]
    description: str
    example: str
    abstract: NotRequired[bool]


# ---------------------------------------------------------------------------
# ERROR CATALOG
# ---------------------------------------------------------------------------
#
# 例外 metadata の単一 source of truth (#199 / #200 / #201).
#
# key は ErrorCode Literal の値 (string literal に統一、live class attr は使わない
# — mixed authoring を避ける #199)。value は ExceptionInfo で、exception_class
# は live class 参照のまま持つ (class rename 時の drift guard; wire 形式への
# 変換は build_schema_payload が担う)。
#
# 新規 ErrorCode 追加時は本 dict と ``ErrorCode`` Literal を必ず同時に更新する。
# drift guard: tests/test_exceptions.py::TestErrorCatalog が public export / entry
# shape / abstract フラグ / Literal との整合を検証する。
#
# 将来、例外クラスが増えて本ファイルが 300 行を超えるようなら CATALOG と class
# 定義を別 module に分離することを検討する (現状は YAGNI で同居)。
ERROR_CATALOG: dict[ErrorCode, ExceptionInfo] = {
    "VAULT_SEARCH_ERROR": {
        "exception_class": VaultSearchError,
        "description": (
            "vault-search-mcp ドメインの基底例外。agent へ直接送出されず、"
            "具体サブクラス (NoteNotFoundError / ValidationError) 経由で発生する。"
        ),
        "example": "(base class; not raised directly to agents)",
        "abstract": True,
    },
    "NOTE_NOT_FOUND": {
        "exception_class": NoteNotFoundError,
        "description": (
            "指定された path の note が index に存在しない。"
            "vault_search や vault_folders で path を先に確認してから再試行する。"
        ),
        "example": "Note not found: Projects/foo.md",
    },
    "VALIDATION_ERROR": {
        "exception_class": ValidationError,
        "description": (
            "エージェント入力の検証失敗 (識別子不正 / ページング範囲外 など)。"
            "より具体的な UNKNOWN_FRONTMATTER_KEY / UNSUPPORTED_RANGE_OPERATOR "
            "で返るケースもある。"
        ),
        "example": "limit must be <= 500 (got 1000)",
    },
    "UNKNOWN_FRONTMATTER_KEY": {
        "exception_class": ValidationError,
        "description": (
            "metadata_filter に未知の frontmatter key を指定。message に "
            "``did you mean: <key>?`` の修正候補 (編集距離 suggest) が付く。"
            "schema://tools の frontmatter_keys で有効キー集合を事前確認できる。"
        ),
        "example": (
            "Unknown frontmatter key 'statu'; did you mean: status? "
            "See schema://tools for the frontmatter_keys list"
        ),
    },
    "UNSUPPORTED_RANGE_OPERATOR": {
        "exception_class": ValidationError,
        "description": (
            "metadata_filter で gt / lt / gte / lte 等の範囲比較演算子を指定。"
            "frontmatter 値は index 時に文字列正規化されるため範囲比較は未対応。"
            "対応演算子は eq (bare string) / in / ne のみ。数値・日付比較は"
            "取得後のクライアント側 post-filter で行う。"
        ),
        "example": (
            "Unsupported operator 'gt' for key 'priority': "
            "numeric/date range comparison is not supported in metadata_filter."
        ),
    },
}
