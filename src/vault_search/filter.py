"""metadata_filter parser/validator and SQL fragment builder (Issue #5).

frontmatter の任意プロパティを AND 条件で絞り込む dict 構文を
バリデーション済みの :class:`MetadataCondition` リストへ変換し、
さらに SQLite 用の WHERE 断片に変換する。

構文:
    {
        "status": "active",                       # 暗黙 eq
        "priority": {"in": ["high", "critical"]}, # in 演算
        "archived": {"ne": "true"},               # ne 演算
    }

不正演算子・不正キー名・不正値はすべて
:class:`~vault_search.validation.ValidationError` を送出する。
エラーメッセージは、エージェントがどのキー・どの演算子を
どう直せばよいかを自己修正できるよう具体的に構成する。
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from .validation import ValidationError, validate_identifier, validate_value

__all__ = ["MetadataCondition", "Operator", "build_sql_fragment", "parse_metadata_filter"]

# 演算子の単一 source of truth。``eq`` / ``ne`` / ``in`` の 3 演算子。
# ``MetadataCondition.op`` と ``_EXPLICIT_OPS`` はここから派生させる。
Operator = Literal["eq", "ne", "in"]

# 明示的に dict 値で指定できる演算子。``eq`` は str 値による暗黙指定のみ。
_EXPLICIT_OPS: tuple[str, ...] = tuple(op for op in get_args(Operator) if op != "eq")

# Numeric / date range comparison aliases. These are not supported in
# metadata_filter (index-time string normalization makes ordered comparison
# semantically undefined); detecting them lets agents self-correct to a
# client-side post-filter instead of retrying aliases like gt/gte/>=. (#87)
_RANGE_OP_ALIASES: frozenset[str] = frozenset(
    {
        "gt",
        "lt",
        "gte",
        "lte",
        ">",
        "<",
        ">=",
        "<=",
        "greater_than",
        "less_than",
        "greater_than_or_equal",
        "less_than_or_equal",
        # Issue #121: natural-language aliases LLMs tend to generate
        "after",
        "before",
        "between",
        "range",
        "from",
        "to",
    }
)


@dataclass(frozen=True)
class MetadataCondition:
    """検証済みの単一 frontmatter 条件.

    Attributes
    ----------
    key:
        frontmatter のトップレベルまたはドット区切りキー。
        :func:`vault_search.validation.validate_identifier` で検証済み
        (各セグメントは ``A-Za-z0-9_-`` のみ、空セグメント不可)。
        SQL へ直接埋め込んでも安全。
    op:
        比較演算子。``eq`` / ``ne`` / ``in`` のいずれか。
        配列型 frontmatter に対しては ``eq`` / ``in`` が「含む」判定、
        ``ne`` が「含まない」判定として働く
        (詳細は :func:`build_sql_fragment` の Semantics 参照)。
    value:
        比較対象値。``eq`` / ``ne`` は ``str``、``in`` は ``tuple[str, ...]``。
        :func:`vault_search.validation.validate_value` で検証済み。
    """

    key: str
    op: Operator
    value: str | tuple[str, ...]


def parse_metadata_filter(
    raw: dict[str, Any] | None,
    known_keys: Sequence[str] | None = None,
) -> list[MetadataCondition]:
    """``metadata_filter`` dict を :class:`MetadataCondition` リストへ変換する.

    - ``None`` または空 dict → 空 list
    - 各キーは :func:`validate_identifier` (kind="frontmatter key") で検証
    - ``known_keys`` が ``None`` 以外の場合、各キーが ``known_keys`` に含まれるか検証
      (識別子形式チェックの**後**に実施)。含まれない場合は
      ``ValidationError(error_code="UNKNOWN_FRONTMATTER_KEY")`` を送出する。
    - str 値 → ``op="eq"``、値は :func:`validate_value` で検証
    - dict 値 → ``{"in": list[str]}`` / ``{"ne": str}`` のみ許可
        - ``in``: 値は非空 list[str]、各要素を :func:`validate_value` で検証
        - ``ne``: 値は str、:func:`validate_value` で検証
    - それ以外の構造や演算子は :class:`ValidationError`
    """
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValidationError(f"metadata_filter must be a dict, got {type(raw).__name__}")

    conditions: list[MetadataCondition] = []
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValidationError(f"metadata_filter key must be a string, got {type(key).__name__}")
        validate_identifier(key, kind="frontmatter key")

        if known_keys is not None and key not in known_keys:
            suggestions = difflib.get_close_matches(key, known_keys, n=3, cutoff=0.6)
            if suggestions:
                suggestion_str = ", ".join(suggestions)
                msg = (
                    f"Unknown frontmatter key {key!r}; "
                    f"did you mean: {suggestion_str}? "
                    f"See schema://tools for the frontmatter_keys list"
                )
            else:
                preview = ", ".join(sorted(known_keys)[:5])
                suffix = ", ..." if len(known_keys) > 5 else ""
                msg = (
                    f"Unknown frontmatter key {key!r}; "
                    f"valid keys include: {preview}{suffix}. "
                    f"See schema://tools for the full list"
                )
            raise ValidationError(
                msg,
                error_code="UNKNOWN_FRONTMATTER_KEY",
                hint="see schema://tools for the frontmatter_keys list",
                did_you_mean=suggestions,
                allowed=sorted(known_keys),
            )

        conditions.append(_parse_entry(key, value))

    return conditions


def _parse_entry(key: str, value: Any) -> MetadataCondition:
    """単一 ``(key, value)`` エントリを :class:`MetadataCondition` に変換."""
    if isinstance(value, str):
        validate_value(value, kind="frontmatter value")
        return MetadataCondition(key=key, op="eq", value=value)

    if isinstance(value, dict):
        return _parse_operator_dict(key, value)

    raise ValidationError(
        f"metadata_filter[{key!r}] must be a string (implicit eq) or a dict "
        f"(explicit operator), got {type(value).__name__}. "
        f"Frontmatter scalars are normalized to strings at index time; "
        f'pass the stringified form (e.g. {{{key!r}: "{value}"}}).'
    )


def _parse_operator_dict(key: str, op_dict: dict[Any, Any]) -> MetadataCondition:
    """``{"op": value}`` 形式の dict を検証して :class:`MetadataCondition` に変換."""
    if len(op_dict) != 1:
        raise ValidationError(
            f"metadata_filter[{key!r}] must have exactly one operator "
            f"(one of: {', '.join(_EXPLICIT_OPS)}); got {len(op_dict)} entries"
        )
    ((op, op_value),) = op_dict.items()

    if op == "eq":
        # eq は str 値による暗黙指定のみ許可
        raise ValidationError(
            f"Unsupported operator 'eq' for key {key!r}; "
            f'use a bare string value for equality (e.g. {{{key!r}: "..."}}) '
            f"or one of: {', '.join(_EXPLICIT_OPS)}"
        )
    if op in _RANGE_OP_ALIASES:
        raise ValidationError(
            f"Unsupported operator {op!r} for key {key!r}: "
            f"numeric/date range comparison is not supported in metadata_filter. "
            f"Retrieve the notes first and apply post-filter on the client side. "
            f"For equality checks use a bare string (implicit eq), ne, or in.",
            error_code="UNSUPPORTED_RANGE_OPERATOR",
            hint=(
                "metadata_filter values are stored as strings (index-time "
                "normalization). Use in/ne/eq for equality, and perform "
                "numeric/date comparison on the client side after retrieval."
            ),
        )
    if op not in _EXPLICIT_OPS:
        raise ValidationError(
            f"Unsupported operator {op!r} for key {key!r}; "
            f"expected one of: {', '.join(_EXPLICIT_OPS)} "
            f"(or bare string for implicit eq)"
        )

    if op == "in":
        return _parse_in(key, op_value)
    # op == "ne"
    return _parse_ne(key, op_value)


def _format_scalar_for_error(v: object) -> str:
    """エラーメッセージ用に非 str スカラーを index 側の正規化形に合わせて文字列化.

    小文字 ``"true"``/``"false"`` は意図的選択: ``str(True)`` は ``"True"`` を
    返すが、parser の ``_normalize_scalar`` は YAML / JSON 慣例に合わせて
    小文字化する (Issue #15 / #49)。エラーヒントも同じ形で示し、エージェントが
    そのままコピペしてリトライできるようにする。bool 判定を先にするのは
    ``isinstance(True, int)`` が True になる Python の罠への対応。
    """
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def _parse_in(key: str, op_value: Any) -> MetadataCondition:
    if not isinstance(op_value, list):
        raise ValidationError(
            f"metadata_filter[{key!r}]['in'] must be a list of strings, "
            f"got {type(op_value).__name__}"
        )
    if not op_value:
        raise ValidationError(f"metadata_filter[{key!r}]['in'] must be a non-empty list")
    validated: list[str] = []
    for idx, item in enumerate(op_value):
        if not isinstance(item, str):
            raise ValidationError(
                f"metadata_filter[{key!r}]['in'][{idx}] must be a string, "
                f"got {type(item).__name__}. Frontmatter scalars are normalized "
                f"to strings at index time; pass the stringified form "
                f'(e.g. "{_format_scalar_for_error(item)}").'
            )
        validate_value(item, kind="frontmatter value")
        validated.append(item)
    return MetadataCondition(key=key, op="in", value=tuple(validated))


def _parse_ne(key: str, op_value: Any) -> MetadataCondition:
    if not isinstance(op_value, str):
        raise ValidationError(
            f"metadata_filter[{key!r}]['ne'] must be a string, "
            f"got {type(op_value).__name__}. Frontmatter scalars are normalized "
            f"to strings at index time; pass the stringified form "
            f'(e.g. {{{key!r}: {{"ne": "{_format_scalar_for_error(op_value)}"}}}}).'
        )
    validate_value(op_value, kind="frontmatter value")
    return MetadataCondition(key=key, op="ne", value=op_value)


# ---------------------------------------------------------------------------
# SQL fragment builder
# ---------------------------------------------------------------------------


def build_sql_fragment(cond: MetadataCondition) -> tuple[str, list[Any]]:
    """単一 :class:`MetadataCondition` を SQLite WHERE 断片とパラメータに変換.

    返り値は ``(sql_fragment, params)``。``sql_fragment`` は先頭に ``AND`` を
    含む文字列で、indexer 側の既存 SQL (``WHERE ...``) に連結する前提。
    参照するテーブル別名は ``n`` (``notes`` テーブル) を仮定する。

    ``cond.key`` は :func:`validate_identifier` 済みの安全な識別子
    (各セグメント ``A-Za-z0-9_-``、空セグメント不可) なので、JSON パス
    ``$.<key>`` に直接埋め込んでも SQL インジェクションも malformed JSON
    path も発生しない。比較対象値は常にプレースホルダ (``?``) で渡すため、
    ユーザ値が SQL 断片に混ざることはない。

    Type invariant
    --------------
    frontmatter 内のスカラー値は :func:`vault_search.parser._normalize_fm`
    により index 時に文字列化されている (Issue #15 / #49)。そのため本関数の
    SQL は単純な str→str 等価比較のみで済む。DB を直接書き換える等で非文字列値が
    混入した場合はマッチしない — 正規化のトラスト境界は parser にある。

    Semantics
    ---------
    * ``eq``: スカラー等価、または frontmatter 側が配列の場合は要素含有
      (例: ``tags: [a, b]`` に ``tags == a`` がマッチ)。
    * ``ne``: キーが存在し、かつ値が「含まれない」場合のみ true。
      - スカラー値: ``json_extract(...) != value``。
      - 配列値: 配列内のどの要素も ``value`` に等しくない場合のみマッチ。
        (例: ``categories: [work, urgent]`` に ``categories != work`` は
        マッチ**しない**。配列内に ``work`` を含むため。)
      キー欠落はマッチ扱いしない (``eq`` との対称性のため)。
    * ``in``: スカラーがリスト内のいずれかに一致、または frontmatter 側が
      配列でリスト要素のいずれかを含む場合。
    """
    json_path = f"$.{cond.key}"

    if cond.op == "eq":
        assert isinstance(cond.value, str)
        fragment = (
            "AND ("
            "json_extract(n.frontmatter, ?) = ? "
            "OR ("
            "  json_type(n.frontmatter, ?) = 'array' "
            "  AND EXISTS ("
            "    SELECT 1 FROM json_each(json_extract(n.frontmatter, ?)) "
            "    WHERE value = ?"
            "  )"
            ")"
            ")"
        )
        params: list[Any] = [json_path, cond.value, json_path, json_path, cond.value]
        return fragment, params

    if cond.op == "ne":
        assert isinstance(cond.value, str)
        fragment = (
            "AND json_extract(n.frontmatter, ?) IS NOT NULL "
            "AND ("
            "(json_type(n.frontmatter, ?) != 'array' "
            " AND json_extract(n.frontmatter, ?) != ?) "
            "OR ("
            "  json_type(n.frontmatter, ?) = 'array' "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM json_each(json_extract(n.frontmatter, ?)) "
            "    WHERE value = ?"
            "  )"
            ")"
            ")"
        )
        params = [
            json_path,  # IS NOT NULL check
            json_path,  # json_type != 'array'
            json_path,  # json_extract != value (scalar branch)
            cond.value,
            json_path,  # json_type = 'array'
            json_path,  # json_each arg
            cond.value,
        ]
        return fragment, params

    # op == "in"
    assert isinstance(cond.value, tuple)
    values = list(cond.value)
    placeholders = ",".join("?" * len(values))
    fragment = (
        "AND ("
        f"json_extract(n.frontmatter, ?) IN ({placeholders}) "
        "OR ("
        "  json_type(n.frontmatter, ?) = 'array' "
        "  AND EXISTS ("
        "    SELECT 1 FROM json_each(json_extract(n.frontmatter, ?)) "
        f"    WHERE value IN ({placeholders})"
        "  )"
        ")"
        ")"
    )
    params = [json_path, *values, json_path, json_path, *values]
    return fragment, params
