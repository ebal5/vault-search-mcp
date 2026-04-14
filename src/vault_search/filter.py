"""metadata_filter parser/validator for vault_search (Issue #5).

frontmatter の任意プロパティを AND 条件で絞り込む dict 構文を
バリデーション済みの :class:`MetadataCondition` リストへ変換する。

構文:
    {
        "status": "active",                       # 暗黙 eq
        "priority": {"in": ["high", "critical"]}, # in 演算
        "archived": {"ne": "true"},               # ne 演算
    }

不正演算子・不正キー名・不正値はすべて
:class:`~vault_search.validation.ValidationError` を送出する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .validation import ValidationError, validate_identifier, validate_value

__all__ = ["MetadataCondition", "parse_metadata_filter"]

_ALLOWED_OPS = ("eq", "ne", "in")


@dataclass(frozen=True)
class MetadataCondition:
    """検証済みの単一 frontmatter 条件."""

    key: str
    op: Literal["eq", "ne", "in"]
    value: str | tuple[str, ...]


def parse_metadata_filter(
    raw: dict[str, Any] | None,
) -> list[MetadataCondition]:
    """``metadata_filter`` dict を :class:`MetadataCondition` リストへ変換する.

    - ``None`` または空 dict → 空 list
    - 各キーは :func:`validate_identifier` (kind="frontmatter key") で検証
    - str 値 → ``op="eq"``、値は :func:`validate_value` で検証
    - dict 値 → ``{"in": list[str]}`` / ``{"ne": str}`` のみ許可
        - ``in``: 値は list[str]、各要素を :func:`validate_value` で検証
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

        if isinstance(value, str):
            validate_value(value, kind="frontmatter value")
            conditions.append(MetadataCondition(key=key, op="eq", value=value))
        elif isinstance(value, dict):
            if len(value) != 1:
                raise ValidationError(
                    f"metadata_filter[{key!r}] must have exactly one operator, got {len(value)}"
                )
            ((op, op_value),) = value.items()
            if op not in _ALLOWED_OPS or op == "eq":
                # eq は明示指定不可 (str 値で暗黙)
                raise ValidationError(
                    f"unsupported operator {op!r} for {key!r}; "
                    f"allowed: 'in', 'ne' (or bare string for eq)"
                )
            if op == "in":
                if not isinstance(op_value, list) or not op_value:
                    raise ValidationError(
                        f"metadata_filter[{key!r}]['in'] must be a non-empty list"
                    )
                validated: list[str] = []
                for item in op_value:
                    if not isinstance(item, str):
                        raise ValidationError(
                            f"metadata_filter[{key!r}]['in'] items must be strings, "
                            f"got {type(item).__name__}"
                        )
                    validate_value(item, kind="frontmatter value")
                    validated.append(item)
                conditions.append(MetadataCondition(key=key, op="in", value=tuple(validated)))
            else:  # op == "ne"
                if not isinstance(op_value, str):
                    raise ValidationError(
                        f"metadata_filter[{key!r}]['ne'] must be a string, "
                        f"got {type(op_value).__name__}"
                    )
                validate_value(op_value, kind="frontmatter value")
                conditions.append(MetadataCondition(key=key, op="ne", value=op_value))
        else:
            raise ValidationError(
                f"metadata_filter[{key!r}] must be a string or dict, got {type(value).__name__}"
            )

    return conditions
