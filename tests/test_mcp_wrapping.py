"""MCP structured content が wrap されないことを検証する regression テスト.

Bug 背景:
    ツール戻り型を ``SearchResponse | dict[str, Any]`` の Union にすると
    FastMCP の ``_try_create_model_and_schema`` が ``wrap_output=True`` を
    設定し、実際の MCP structured content / outputSchema が
    ``{"result": <payload>}`` と 1 段ラップされてしまう。
    一方 ``schema://tools`` が公開する output_schema は SearchResponse の
    直接形 (tier / total / results) のままなので、エージェント側で
    スキーマと実レスポンスが drift し KeyError を招く。

本テストは tool 戻り型を dict に統一する修正の Red フェーズとして失敗し、
Green フェーズで通るようになる。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vault_search import server as server_mod
from vault_search.indexer import VaultIndex
from vault_search.schemas import build_schema_payload


@pytest.fixture(autouse=True)
def _inject_index(vault_index: VaultIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "_index", vault_index)


def _call_tool(tool_name: str, arguments: dict[str, Any]) -> tuple[Any, Any]:
    return asyncio.run(server_mod.mcp.call_tool(tool_name, arguments))


def test_vault_search_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_search の structured content が ``result`` でラップされない."""
    _content, structured = _call_tool("vault_search", {"query": "obsidian"})
    assert isinstance(structured, dict)
    assert "tier" in structured, f"structured wrapped: keys={list(structured.keys())}"
    assert "total" in structured
    assert "results" in structured
    # "result" でラップされていないこと
    if "result" in structured:
        inner = structured["result"]
        assert not (isinstance(inner, dict) and "tier" in inner), (
            f"structured wrapped via 'result' key: {structured}"
        )


def test_vault_search_fields_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """fields 指定時も structured content が wrap されない."""
    _content, structured = _call_tool(
        "vault_search",
        {"query": "obsidian", "fields": ["path", "title"]},
    )
    assert isinstance(structured, dict)
    assert set(structured.keys()) >= {"tier", "total", "results"}
    for hit in structured["results"]:
        assert set(hit.keys()) == {"path", "title"}


def test_vault_get_note_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_get_note の structured content が wrap されない."""
    _content, structured = _call_tool("vault_get_note", {"path": "Welcome.md"})
    assert isinstance(structured, dict)
    # NoteDetail の必須キーを直接持つ (wrap されていない)
    assert "path" in structured
    assert "title" in structured


def test_vault_recent_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_recent (envelope dict 戻り) の structured content が wrap されない.

    旧仕様では ``list[dict]`` を返していたため FastMCP が ``{"result": [...]}``
    にラップしていた。現仕様は ``{"notes": [...]}`` envelope を直接返す。
    """
    _content, structured = _call_tool("vault_recent", {"limit": 3})
    assert isinstance(structured, dict)
    assert "result" not in structured, (
        f"vault_recent wrap drift: keys={list(structured.keys())}"
    )
    assert "notes" in structured, f"envelope key 'notes' missing: {structured!r}"
    assert isinstance(structured["notes"], list)


def test_vault_tags_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_tags (envelope dict 戻り) の structured content が wrap されない."""
    _content, structured = _call_tool("vault_tags", {})
    assert isinstance(structured, dict)
    assert "result" not in structured, (
        f"vault_tags wrap drift: keys={list(structured.keys())}"
    )
    assert "tags" in structured, f"envelope key 'tags' missing: {structured!r}"
    assert isinstance(structured["tags"], list)


def test_vault_folders_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_folders (envelope dict 戻り) の structured content が wrap されない."""
    _content, structured = _call_tool("vault_folders", {})
    assert isinstance(structured, dict)
    assert "result" not in structured, (
        f"vault_folders wrap drift: keys={list(structured.keys())}"
    )
    assert "folders" in structured, f"envelope key 'folders' missing: {structured!r}"
    assert isinstance(structured["folders"], list)


def test_vault_search_outputschema_top_level_is_flat(vault_index: VaultIndex) -> None:
    """MCP tools/list outputSchema のトップが tier/total/results を直接含む.

    ``result`` ラッパーキーが挟まっていないことを確認する。
    """
    tools = asyncio.run(server_mod.mcp.list_tools())
    tool = next(t for t in tools if t.name == "vault_search")
    output_schema = tool.outputSchema
    assert isinstance(output_schema, dict)
    props = output_schema.get("properties", {})
    # 理想形: SearchResponse の直接形 (tier/total/results)
    # 許容形: dict ベースの generic object (properties が空 or {"result": ...} 以外)
    # NG 形: {"result": ...} ラッパーのみ
    if set(props.keys()) == {"result"}:
        pytest.fail(f"outputSchema wraps payload in 'result': {output_schema}")


def test_schema_resource_output_schema_remains_rich(vault_index: VaultIndex) -> None:
    """schema://tools の output_schema は SearchResponse の rich schema を維持."""
    payload = build_schema_payload(vault_index)
    output_schema = payload["tools"]["vault_search"]["output_schema"]
    props = output_schema.get("properties", {})
    # 最低でも tier / total / results を直接キーとして持つこと
    assert {"tier", "total", "results"}.issubset(props.keys()), (
        f"schema resource output_schema lost rich SearchResponse shape: {props.keys()}"
    )
