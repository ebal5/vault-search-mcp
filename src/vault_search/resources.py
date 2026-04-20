"""MCP リソース payload の runtime 組み立て.

``mcp_contract.py`` は tool 契約 (schema 生成) に専念し、実データを wire 形式へ
serialize する責務は本 module が担う (Issue #184)。``schema://tools`` resource
handler は本 module の関数を呼び出すだけ。

Payload の top-level に直接乗る module-level 定数 (``_SCHEMA_VERSION`` /
``_OVERVIEW`` / ``_RECOMMENDED_FLOW`` / ``_ERRORS_WIRE_FORMAT_NOTE`` /
``_VERSION_POLICY`` / ``_FRONTMATTER_KEY_INFO_SCHEMA``) は ``payload_meta.py``
に分離した (Issue #195)。本 module は組立 entry point (``build_schema_payload``)
と、wire 形式への transformer (``_serialize_error_catalog``) のみを保持する。

言語方針 (ja-JP 固定) / prose の drift guard 方針 / Option A (package 化)
移行 tripwire は ``payload_meta.py`` の module docstring が単一 SoT。

## Read-only 契約

``build_schema_payload`` が返す dict は ``_RECOMMENDED_FLOW`` / ``TOOL_ENTRIES``
等の module-level 定数への直接参照を含む (``errors`` は
``_serialize_error_catalog()`` が毎回新 dict を作るため share されない)。
呼出側は返り値を **read-only として扱うこと** — mutate すると以降の呼出
および他セッションに波及する。MCP resource 経路は即座に JSON serialize する
ためこの制約で実害は出ないが、future 拡張 (test や非 MCP 呼出) での罠を
避けるため明記する。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .exceptions import ERROR_CATALOG
from .mcp_contract import TOOL_ENTRIES
from .payload_meta import (
    _ERRORS_WIRE_FORMAT_NOTE,
    _FRONTMATTER_KEY_INFO_SCHEMA,
    _OVERVIEW,
    _RECOMMENDED_FLOW,
    _SCHEMA_VERSION,
    _VERSION_POLICY,
)
from .schema_meta import FrontmatterKeyInfo

__all__ = ["build_schema_payload"]


# 全 error entry に付与する共通の wire-format prefix template (#214).
#
# entry に直接 dict access した agent が top-level errors_wire_format_note を
# 経由せずに FastMCP の wrap 形式を理解できるようにするための backreference。
# ``<tool>`` は呼び出し時の MCP tool 名 (例: vault_search) に置換される placeholder。
# agent は ``wire_prefix.replace("<tool>", name) + example`` で期待 wire
# message を構築できる。値は全 entry 共通の定数なので、将来の FastMCP wrap 形式
# 変更時は本定数 1 箇所だけ更新する。
_ERROR_WIRE_PREFIX_TEMPLATE: str = "Error executing tool <tool>: "


def _serialize_error_catalog() -> dict[str, dict[str, str]]:
    """``ERROR_CATALOG`` を agent-facing wire 形式に変換する (#199 / #200 / #201 / #214).

    * ``exception_class`` は live class 参照なので ``__name__`` に展開する
      (raised_by)
    * ``abstract=True`` の entry は除外 — ``VaultSearchError`` のような基底例外
      が agent の pattern-match 対象に混ざらないようにする (#200)
    * 戻り dict の key は ``ErrorCode`` Literal 値と一致する (string literal に
      統一、live class attr は参照しない #199)
    * 各 entry に共通の ``wire_prefix`` を付与し、entry 単独で wire 形式を
      理解可能にする (#214 — top-level ``errors_wire_format_note`` への
      backreference)
    """
    return {
        code: {
            "raised_by": info["exception_class"].__name__,
            "description": info["description"],
            "example": info["example"],
            "wire_prefix": _ERROR_WIRE_PREFIX_TEMPLATE,
        }
        for code, info in ERROR_CATALOG.items()
        if not info.get("abstract", False)
    }


def build_schema_payload(
    frontmatter_keys: Iterable[FrontmatterKeyInfo],
) -> dict[str, Any]:
    """schema://tools resource payload を組み立てる.

    呼び出し側は ``VaultIndex.list_frontmatter_keys()`` 相当の反復可能オブジェクトを
    そのまま渡す。Pydantic モデルを wire 形式の dict へ serialize するのは
    resource layer (本 module) の責務。
    """
    return {
        "version": _SCHEMA_VERSION,
        "version_policy": _VERSION_POLICY,
        "overview": _OVERVIEW,
        "recommended_flow": _RECOMMENDED_FLOW,
        "errors_wire_format_note": _ERRORS_WIRE_FORMAT_NOTE,
        "errors": _serialize_error_catalog(),
        "tools": TOOL_ENTRIES,
        # frontmatter_key_info_schema を frontmatter_keys より前に置くことで
        # agent が value_type の許容値 enum を先に読んでから実データを解釈できる。
        "frontmatter_key_info_schema": _FRONTMATTER_KEY_INFO_SCHEMA,
        "frontmatter_keys": [item.model_dump(mode="json") for item in frontmatter_keys],
    }
