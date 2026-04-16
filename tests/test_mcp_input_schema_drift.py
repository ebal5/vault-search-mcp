"""folder パラメータ input schema の drift 検知テスト (Issue #47).

MCP tools/list が公開する folder input schema の description と
TOOL_SPECS (schema://tools 経路) の description が一致することを assert する。

Red: FastMCP の生成 schema に folder description がない (str | None の plain annotation)
     ため TOOL_SPECS の description と不一致 → テスト失敗。
Green: server.py で Annotated[str | None, Field(description=_FOLDER_DESCRIPTION)] を使い
       mcp_contract.py の _FOLDER_INPUT_SCHEMA を廃止すると両者が一致 → テスト通過。
"""

from __future__ import annotations

import asyncio

from vault_search import server as server_mod
from vault_search.mcp_contract import TOOL_SPECS

# folder パラメータを持つツール
_FOLDER_TOOLS = ["vault_search", "vault_recent"]


def _get_fastmcp_input_props() -> dict[str, dict]:
    """FastMCP が tools/list で公開する input schema の properties を全 tool 分返す."""
    tools = asyncio.run(server_mod.mcp.list_tools())
    return {t.name: t.inputSchema.get("properties", {}) for t in tools}


def test_folder_description_present_in_fastmcp_schema() -> None:
    """FastMCP が tools/list で公開する input schema に folder の description が存在すること.

    ``folder: str | None = None`` の plain annotation では description が生成されないため、
    ``Annotated[str | None, Field(description=...)]`` への変更後に pass する (Green)。
    """
    all_props = _get_fastmcp_input_props()
    for tool_name in _FOLDER_TOOLS:
        props = all_props[tool_name]
        assert "folder" in props, f"{tool_name}: folder not in FastMCP input schema"
        desc = props["folder"].get("description", "")
        assert isinstance(desc, str) and desc.strip(), (
            f"{tool_name}: folder description absent in FastMCP input schema. "
            f"folder schema: {props['folder']!r}"
        )


def test_folder_description_matches_tool_specs() -> None:
    """FastMCP input schema の folder description が TOOL_SPECS と一致すること (drift 検知).

    _FOLDER_INPUT_SCHEMA (TOOL_SPECS 側) と server.py の Annotated annotation が
    別々に管理されていると description が drift する。単一ソース化後は両者が
    同一定数を参照するため不一致が起きない。
    """
    all_props = _get_fastmcp_input_props()
    for tool_name in _FOLDER_TOOLS:
        fastmcp_props = all_props[tool_name]
        fastmcp_desc = fastmcp_props.get("folder", {}).get("description", "")

        contract_props = TOOL_SPECS[tool_name].input_schema.get("properties", {})
        contract_desc = contract_props.get("folder", {}).get("description", "")

        assert fastmcp_desc == contract_desc, (
            f"{tool_name}: folder description drift detected\n"
            f"  FastMCP (tools/list): {fastmcp_desc!r}\n"
            f"  TOOL_SPECS (schema://tools): {contract_desc!r}"
        )
        assert contract_desc, f"{tool_name}: TOOL_SPECS folder description must be non-empty"
