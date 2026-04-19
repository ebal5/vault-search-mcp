"""MCP リソース payload の runtime 組み立て.

`mcp_contract.py` は tool 契約 (schema 生成) に専念し、実データを wire 形式へ
serialize する責務は本 module が担う (Issue #184)。`schema://tools` resource
handler は本 module の関数を呼び出すだけ。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .mcp_contract import TOOL_ENTRIES
from .schema_meta import FrontmatterKeyInfo

__all__ = ["build_schema_payload"]


def build_schema_payload(
    frontmatter_keys: Iterable[FrontmatterKeyInfo],
) -> dict[str, Any]:
    """schema://tools resource payload を組み立てる.

    呼び出し側は ``VaultIndex.list_frontmatter_keys()`` 相当の反復可能オブジェクトを
    そのまま渡す。Pydantic モデルを wire 形式の dict へ serialize するのは
    resource layer (本 module) の責務。
    """
    return {
        "tools": TOOL_ENTRIES,
        "frontmatter_keys": [item.model_dump(mode="json") for item in frontmatter_keys],
    }
