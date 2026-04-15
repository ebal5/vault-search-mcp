"""`schema://tools` MCP resource のテスト (Red フェーズ).

Runtime Schema Introspection 原則に従い、AI エージェントが初回接続時に
全 MCP tool の入出力スキーマを取得できるようにする機能の失敗テスト。

検証方針:
- スナップショット / 文字列完全一致は禁止。構造 (キー存在, 型, 部分文字列)
  のみを確認することで、スキーマ文言の微調整で壊れないテストにする。
"""

from __future__ import annotations

from typing import Any

import pytest

from vault_search.indexer import VaultIndex

# 公開対象となる MCP tool 名。server.py の @mcp.tool() 登録と一致させる。
EXPECTED_TOOL_NAMES = {
    "vault_search",
    "vault_get_note",
    "vault_recent",
    "vault_tags",
    "vault_folders",
    "vault_reindex",
    "vault_stats",
}

# SearchHit の全フィールド。schemas.SearchHit と一致させる。
SEARCH_HIT_FIELDS = {
    "path",
    "title",
    "folder",
    "tags",
    "snippet",
    "score",
    "created_at",
    "modified_at",
}


def _search_hit_properties(vault_search_output_schema: dict[str, Any]) -> dict[str, Any]:
    """vault_search の output_schema から SearchHit の properties を取り出す.

    SearchResponse は ``results: list[SearchHit]`` を持つため Pydantic は
    ``$defs.SearchHit`` に SearchHit を切り出す。PR #92 で fields 削除後は
    anyOf ブランチ等が発生しないため直接パスで取り出せる。
    """
    return vault_search_output_schema["$defs"]["SearchHit"]["properties"]


# ---------------------------------------------------------------------------
# build_schema_payload (schemas.py に追加予定)
# ---------------------------------------------------------------------------


def test_build_schema_payload_returns_dict(vault_index: VaultIndex) -> None:
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    assert isinstance(payload, dict)


def test_build_schema_payload_has_tools_key(vault_index: VaultIndex) -> None:
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    assert "tools" in payload
    assert isinstance(payload["tools"], dict)


def test_build_schema_payload_contains_all_tool_names(vault_index: VaultIndex) -> None:
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    tool_names = set(payload["tools"].keys())
    missing = EXPECTED_TOOL_NAMES - tool_names
    assert not missing, f"Missing tools in payload: {missing}"


def test_each_tool_entry_has_input_and_output_schema(vault_index: VaultIndex) -> None:
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    for name in EXPECTED_TOOL_NAMES:
        entry = payload["tools"][name]
        assert isinstance(entry, dict), f"{name} entry is not dict"
        assert "input_schema" in entry, f"{name} missing input_schema"
        assert "output_schema" in entry, f"{name} missing output_schema"
        assert isinstance(entry["input_schema"], dict)
        assert isinstance(entry["output_schema"], dict)


def test_vault_search_output_schema_describes_search_hit_fields(
    vault_index: VaultIndex,
) -> None:
    """SearchHit の全フィールドが vault_search の output_schema 内に存在し、
    それぞれ非空文字列の description を持つことを確認。
    """
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    out_schema = payload["tools"]["vault_search"]["output_schema"]
    props = _search_hit_properties(out_schema)
    assert props, f"No properties found in output_schema: {out_schema}"

    for field in SEARCH_HIT_FIELDS:
        assert field in props, f"SearchHit field '{field}' not in output_schema"
        desc = props[field].get("description")
        assert isinstance(desc, str) and desc.strip(), (
            f"field '{field}' lacks description: {props[field]!r}"
        )


def test_frontmatter_keys_listed(vault_index: VaultIndex) -> None:
    """ルートに 'frontmatter_keys' が list[str] として存在し、
    tmp_vault の Welcome.md に含まれるキーを少なくとも含むこと。
    """
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    assert "frontmatter_keys" in payload
    keys = payload["frontmatter_keys"]
    assert isinstance(keys, list)
    assert all(isinstance(k, str) for k in keys)

    expected_from_welcome = {"title", "tags", "aliases", "created_at", "modified_at"}
    # created_at / modified_at は conftest の Welcome.md で使われているキー名。
    # 実装側が created / modified へ正規化する可能性もあるので
    # 「少なくとも title / tags / aliases と 日付系 1つ以上」を要求。
    assert {"title", "tags", "aliases"}.issubset(set(keys)), (
        f"expected at least title/tags/aliases in frontmatter_keys, got {keys}"
    )
    date_keys = {"created_at", "modified_at", "created", "modified"}
    assert date_keys & set(keys), (
        f"expected at least one date-like key in frontmatter_keys, got {keys}"
    )
    # 参照だけしておく (将来の拡張用)
    _ = expected_from_welcome


# ---------------------------------------------------------------------------
# FastMCP resource 登録
# ---------------------------------------------------------------------------


def test_server_exposes_schema_resource_handler(
    vault_index: VaultIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """server モジュールに `schema_resource` が定義されており、呼び出すと
    build_schema_payload と同じ dict を返すこと。
    """
    from vault_search import server as server_mod
    from vault_search.schemas import build_schema_payload

    monkeypatch.setattr(server_mod, "_index", vault_index)

    assert hasattr(server_mod, "schema_resource"), (
        "server module must expose a `schema_resource` handler"
    )
    handler = server_mod.schema_resource
    assert callable(handler), "schema_resource must be callable"

    result = handler()
    expected = build_schema_payload(vault_index)

    assert isinstance(result, dict)
    assert set(result.keys()) == set(expected.keys())
    assert set(result["tools"].keys()) == set(expected["tools"].keys())


def test_schema_resource_registered_in_fastmcp(vault_index: VaultIndex) -> None:
    """FastMCP インスタンスに schema://tools URI で resource が登録されていること."""
    import asyncio

    from vault_search import server as server_mod

    resources = asyncio.run(server_mod.mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert any("schema://tools" in u for u in uris), (
        f"schema://tools resource not registered; got URIs: {uris}"
    )


def test_list_returning_tools_have_envelope_output_schema(vault_index: VaultIndex) -> None:
    """vault_tags / vault_folders / vault_recent の output_schema が envelope object.

    旧仕様では ``{"type": "array", "items": {...}}`` のフラット形を公開していたが、
    FastMCP が list 戻りを ``{"result": [...]}`` でラップするため実レスポンスと
    乖離していた。現仕様は ``dict`` envelope に統一:
    - vault_tags    -> {"tags":    [TagCount,    ...]}
    - vault_folders -> {"folders": [FolderCount, ...]}
    - vault_recent  -> {"notes":   [RecentNote,  ...]}
    """
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    for tool_name, env_key in [
        ("vault_tags", "tags"),
        ("vault_folders", "folders"),
        ("vault_recent", "notes"),
    ]:
        oschema = payload["tools"][tool_name]["output_schema"]
        assert oschema.get("type") == "object", (
            f"{tool_name} output_schema must be envelope object, got: {oschema!r}"
        )
        props = oschema.get("properties", {})
        assert env_key in props, f"{tool_name} envelope missing '{env_key}': {props!r}"
        assert props[env_key].get("type") == "array", (
            f"{tool_name}.{env_key} must be array, got: {props[env_key]!r}"
        )
        # array の items は該当モデルの JSON schema (properties を直接 or $defs 経由で持つ)
        items = props[env_key].get("items")
        assert isinstance(items, dict) and items, (
            f"{tool_name}.{env_key}.items schema missing: {props[env_key]!r}"
        )


def test_metadata_filter_grammar_structured_in_schema(vault_index: VaultIndex) -> None:
    """metadata_filter の additionalProperties が oneOf 構造で演算子 grammar を表現すること.

    演算子 dict の構造 (`{"in": [...]}` / `{"ne": "..."}`) が機械可読な形で
    JSON Schema に露出していないと、エージェントが MongoDB 風の `$in` や
    `{"eq": "..."}` をハルシネーションしてしまう。
    """
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    mf_schema = payload["tools"]["vault_search"]["input_schema"]["properties"]["metadata_filter"]

    ap = mf_schema.get("additionalProperties")
    assert isinstance(ap, dict), "additionalProperties should be a schema object, not bool"
    assert "oneOf" in ap, f"additionalProperties must expose oneOf variants: {ap!r}"
    oneof = ap["oneOf"]
    assert isinstance(oneof, list)
    assert len(oneof) == 3, f"expected 3 variants (string / in / ne), got {len(oneof)}: {oneof!r}"

    types = {variant.get("type") for variant in oneof}
    assert "string" in types, f"bare string (implicit eq) variant missing: {oneof!r}"

    obj_variants = [v for v in oneof if v.get("type") == "object"]
    assert len(obj_variants) == 2, f"expected 2 object variants (in, ne): {obj_variants!r}"
    ops = [set(v.get("properties", {}).keys()) for v in obj_variants]
    assert {"in"} in ops, f"'in' operator variant missing: {ops!r}"
    assert {"ne"} in ops, f"'ne' operator variant missing: {ops!r}"


# ---------------------------------------------------------------------------
# vault_get_note output_schema の shape 検証
# ---------------------------------------------------------------------------


def test_vault_get_note_output_schema_has_top_level_object_shape(vault_index: VaultIndex) -> None:
    """vault_get_note の output_schema がトップレベルで ``type: object`` を持つこと.

    全 7 ツールで ``{"type": "object", "properties": {...}}`` 形を維持する。
    エージェントの schema クローラが ``type == 'object'`` 前提のことがあるため。
    """
    from vault_search.schemas import build_schema_payload

    payload = build_schema_payload(vault_index)
    schema = payload["tools"]["vault_get_note"]["output_schema"]

    assert schema.get("type") == "object", (
        f"vault_get_note top-level must be object shape; got keys={list(schema.keys())}"
    )
    assert "properties" in schema, (
        f"vault_get_note top-level must expose properties; got keys={list(schema.keys())}"
    )
    assert "path" in schema["properties"], (
        f"vault_get_note properties must include 'path'; got {list(schema['properties'].keys())}"
    )
