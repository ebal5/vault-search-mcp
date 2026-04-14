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

import jsonschema
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
    assert "result" not in structured, f"vault_recent wrap drift: keys={list(structured.keys())}"
    assert "notes" in structured, f"envelope key 'notes' missing: {structured!r}"
    assert isinstance(structured["notes"], list)


def test_vault_tags_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_tags (envelope dict 戻り) の structured content が wrap されない."""
    _content, structured = _call_tool("vault_tags", {})
    assert isinstance(structured, dict)
    assert "result" not in structured, f"vault_tags wrap drift: keys={list(structured.keys())}"
    assert "tags" in structured, f"envelope key 'tags' missing: {structured!r}"
    assert isinstance(structured["tags"], list)


def test_vault_folders_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_folders (envelope dict 戻り) の structured content が wrap されない."""
    _content, structured = _call_tool("vault_folders", {})
    assert isinstance(structured, dict)
    assert "result" not in structured, f"vault_folders wrap drift: keys={list(structured.keys())}"
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


def _get_tool_output_schema(tool_name: str) -> dict[str, Any]:
    tools = asyncio.run(server_mod.mcp.list_tools())
    tool = next(t for t in tools if t.name == tool_name)
    assert tool.outputSchema is not None
    return tool.outputSchema


def test_mcp_vault_search_fields_subset_passes_lowlevel_validation(
    vault_index: VaultIndex,
) -> None:
    """fields 指定 subset が MCP outputSchema で jsonschema.validate パスすること.

    MCP lowlevel server (mcp/server/lowlevel/server.py) は structured content に
    対し outputSchema で jsonschema.validate を強制する。rich schema が
    required=[path, title, folder, ...] を持っていると fields=["path","title"]
    の subset が required 違反で拒否される regression を検出する。
    """
    _content, structured = _call_tool(
        "vault_search",
        {"query": "obsidian", "fields": ["path", "title"]},
    )
    schema = _get_tool_output_schema("vault_search")
    jsonschema.validate(structured, schema)


def test_mcp_vault_search_full_response_validates(vault_index: VaultIndex) -> None:
    """fields=None でも schema が緩みすぎないこと (required 維持)."""
    _content, structured = _call_tool("vault_search", {"query": "obsidian"})
    schema = _get_tool_output_schema("vault_search")
    jsonschema.validate(structured, schema)
    # 少なくとも tier/total は required のまま (schema が緩みすぎていない)
    assert set(schema.get("required", [])) >= {"tier", "total"}


def test_mcp_vault_get_note_fields_subset_passes_lowlevel_validation(
    vault_index: VaultIndex,
) -> None:
    """vault_get_note の fields subset が jsonschema.validate パス."""
    _content, structured = _call_tool(
        "vault_get_note",
        {"path": "Welcome.md", "fields": ["path", "title"]},
    )
    schema = _get_tool_output_schema("vault_get_note")
    jsonschema.validate(structured, schema)


def test_mcp_vault_get_note_full_response_validates(vault_index: VaultIndex) -> None:
    _content, structured = _call_tool("vault_get_note", {"path": "Welcome.md"})
    schema = _get_tool_output_schema("vault_get_note")
    jsonschema.validate(structured, schema)


def test_mcp_vault_recent_fields_subset_passes_lowlevel_validation(
    vault_index: VaultIndex,
) -> None:
    """vault_recent envelope の items が fields subset でも jsonschema.validate パス."""
    _content, structured = _call_tool(
        "vault_recent",
        {"limit": 3, "fields": ["path", "title"]},
    )
    schema = _get_tool_output_schema("vault_recent")
    jsonschema.validate(structured, schema)


def test_mcp_vault_recent_full_response_validates(vault_index: VaultIndex) -> None:
    _content, structured = _call_tool("vault_recent", {"limit": 3})
    schema = _get_tool_output_schema("vault_recent")
    jsonschema.validate(structured, schema)


def test_vault_reindex_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_reindex の structured content が ``result`` でラップされない.

    ReindexStats (Pydantic) 戻り型のままだと FastMCP のバージョン依存で
    wrap_output の挙動が変わる (silent regression 導線)。dict 統一後は
    wrap が発生しないことを保証する。
    """
    _content, structured = _call_tool("vault_reindex", {})
    assert isinstance(structured, dict)
    assert "result" not in structured, f"vault_reindex wrap drift: keys={list(structured.keys())}"
    assert {"added", "updated", "deleted", "skipped", "errors"}.issubset(structured.keys()), (
        f"vault_reindex missing ReindexStats keys: {structured!r}"
    )
    schema = _get_tool_output_schema("vault_reindex")
    jsonschema.validate(structured, schema)


def test_vault_stats_structured_content_not_wrapped(vault_index: VaultIndex) -> None:
    """vault_stats の structured content が ``result`` でラップされない."""
    _content, structured = _call_tool("vault_stats", {})
    assert isinstance(structured, dict)
    assert "result" not in structured, f"vault_stats wrap drift: keys={list(structured.keys())}"
    assert {"total_notes", "db_size_bytes", "db_size_mb", "vault_root"}.issubset(
        structured.keys()
    ), f"vault_stats missing VaultStats keys: {structured!r}"
    schema = _get_tool_output_schema("vault_stats")
    jsonschema.validate(structured, schema)


def test_mcp_outputschema_is_rich_matches_resource(vault_index: VaultIndex) -> None:
    """MCP tools/list の outputSchema が schema://tools と同じ rich schema であること.

    Round 3 で dict[str, Any] 戻り型に統一した結果、FastMCP 自動生成の outputSchema
    が ``{"additionalProperties": True, "type": "object"}`` 相当の空 schema になり、
    schema://tools と カノニカルソースが 2 つに分裂していた。本テストは
    両者の properties キー集合が一致することを保証する regression。
    """

    def _top_props(schema: dict[str, Any]) -> dict[str, Any]:
        """anyOf でラップされた schema からも full 分岐の properties を取り出す."""
        if "properties" in schema:
            return schema["properties"]
        if "anyOf" in schema and schema["anyOf"]:
            return schema["anyOf"][0].get("properties", {})
        return {}

    tools = asyncio.run(server_mod.mcp.list_tools())
    payload = build_schema_payload(vault_index)
    for tool in tools:
        assert tool.outputSchema is not None, f"{tool.name}: outputSchema missing"
        resource_schema = payload["tools"][tool.name]["output_schema"]
        props = _top_props(tool.outputSchema)
        assert props, (
            f"{tool.name}: MCP outputSchema has no properties (empty schema): {tool.outputSchema}"
        )
        resource_props = _top_props(resource_schema)
        assert set(props.keys()) == set(resource_props.keys()), (
            f"{tool.name}: MCP outputSchema keys {set(props.keys())} "
            f"!= resource schema keys {set(resource_props.keys())}"
        )
