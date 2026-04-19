"""`schema://tools` MCP resource のテスト.

Runtime Schema Introspection 原則に従い、AI エージェントが初回接続時に
全 MCP tool の入出力スキーマを取得できるようにする機能を検証する。

検証方針:
- スナップショット / 文字列完全一致は禁止。構造 (キー存在, 型, 部分文字列)
  のみを確認することで、スキーマ文言の微調整で壊れないテストにする。
"""

from __future__ import annotations

import asyncio
import json
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
# build_schema_payload (resources.py)
# ---------------------------------------------------------------------------


def test_build_schema_payload_returns_dict(vault_index: VaultIndex) -> None:
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert isinstance(payload, dict)


def test_build_schema_payload_has_tools_key(vault_index: VaultIndex) -> None:
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert "tools" in payload
    assert isinstance(payload["tools"], dict)


def test_build_schema_payload_contains_all_tool_names(vault_index: VaultIndex) -> None:
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    tool_names = set(payload["tools"].keys())
    missing = EXPECTED_TOOL_NAMES - tool_names
    assert not missing, f"Missing tools in payload: {missing}"


def test_each_tool_entry_has_input_and_output_schema(vault_index: VaultIndex) -> None:
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
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
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
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
    """ルートに 'frontmatter_keys' が list[dict] として存在し、
    tmp_vault の Welcome.md に含まれるキーを少なくとも含むこと。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert "frontmatter_keys" in payload
    keys = payload["frontmatter_keys"]
    assert isinstance(keys, list)
    assert all(isinstance(k, dict) for k in keys)

    # 各 dict に必須フィールドが含まれること
    for info in keys:
        assert "key" in info, f"'key' フィールドが欠落: {info!r}"
        assert "value_type" in info, f"'value_type' フィールドが欠落: {info!r}"
        assert "note_count" in info, f"'note_count' フィールドが欠落: {info!r}"

    key_names = {info["key"] for info in keys}

    expected_from_welcome = {"title", "tags", "aliases", "created_at", "modified_at"}
    # created_at / modified_at は conftest の Welcome.md で使われているキー名。
    # 実装側が created / modified へ正規化する可能性もあるので
    # 「少なくとも title / tags / aliases と 日付系 1つ以上」を要求。
    assert {"title", "tags", "aliases"}.issubset(key_names), (
        f"expected at least title/tags/aliases in frontmatter_keys, got {key_names}"
    )
    date_keys = {"created_at", "modified_at", "created", "modified"}
    assert date_keys & key_names, (
        f"expected at least one date-like key in frontmatter_keys, got {key_names}"
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
    from vault_search.resources import build_schema_payload

    monkeypatch.setattr(server_mod, "_index", vault_index)

    assert hasattr(server_mod, "schema_resource"), (
        "server module must expose a `schema_resource` handler"
    )
    handler = server_mod.schema_resource
    assert callable(handler), "schema_resource must be callable"

    result = handler()
    expected = build_schema_payload(vault_index.list_frontmatter_keys())

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
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
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
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
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
# ne 演算子スキーマの 3 値論理明示 (Issue #50)
# ---------------------------------------------------------------------------


def test_ne_operator_description_mentions_missing_key_behavior(vault_index: VaultIndex) -> None:
    """ne 演算子の schema 内 description にキー欠落はマッチしないことが明記されること.

    ``{"status": {"ne": "active"}}`` で status キーを持たないノートが除外される
    3 値論理は直感に反する。エージェントが「ne が効かない」と誤診しないよう、
    ne プロパティ自身の description に挙動を明示する (Issue #50)。

    確認項目:
    - ne プロパティが description を持つ
    - description に「キー欠落」「存在しない」「3値」「missing」のいずれかを含む
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    mf_schema = payload["tools"]["vault_search"]["input_schema"]["properties"]["metadata_filter"]
    ap = mf_schema["additionalProperties"]
    oneof = ap["oneOf"]

    ne_variant = next(
        (v for v in oneof if v.get("type") == "object" and "ne" in v.get("properties", {})),
        None,
    )
    assert ne_variant is not None, "ne operator variant not found in oneOf"

    ne_schema = ne_variant["properties"]["ne"]
    desc = ne_schema.get("description", "")
    assert isinstance(desc, str) and desc.strip(), (
        "ne property schema must have a non-empty description"
    )
    keywords = ("3値", "キー欠落", "存在しない", "missing")
    assert any(kw in desc for kw in keywords), (
        f"ne description must mention 3-value logic or missing-key behavior "
        f"(expected one of {keywords}): {desc!r}"
    )


# ---------------------------------------------------------------------------
# vault_get_note output_schema の shape 検証
# ---------------------------------------------------------------------------


def test_vault_get_note_output_schema_has_top_level_object_shape(vault_index: VaultIndex) -> None:
    """vault_get_note の output_schema がトップレベルで ``type: object`` を持つこと.

    全 7 ツールで ``{"type": "object", "properties": {...}}`` 形を維持する。
    エージェントの schema クローラが ``type == 'object'`` 前提のことがあるため。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
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


# ---------------------------------------------------------------------------
# Issue #25: read_resource 経路での runtime 検証
# ---------------------------------------------------------------------------


def test_read_resource_schema_tools_returns_payload(
    vault_index: VaultIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_resource("schema://tools") が build_schema_payload() と同じ JSON を返す.

    URI 登録チェックだけでなく実際の read_resource 呼び出しまで通す regression guard。
    """
    from vault_search import server as server_mod
    from vault_search.resources import build_schema_payload

    monkeypatch.setattr(server_mod, "_index", vault_index)

    contents = asyncio.run(server_mod.mcp.read_resource("schema://tools"))
    items = list(contents)
    assert len(items) == 1, f"expected 1 content item, got {len(items)}"

    payload = json.loads(items[0].content)
    expected = build_schema_payload(vault_index.list_frontmatter_keys())
    assert payload == expected, (
        f"read_resource payload differs from build_schema_payload:\n"
        f"  got keys: {list(payload.keys())}\n"
        f"  expected keys: {list(expected.keys())}"
    )


def test_read_resource_schema_tools_uninitialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_index が None (未初期化) のとき read_resource("schema://tools") がエラーを送出する.

    FastMCP は resource 関数内の例外を ResourceError にラップして再送出する。
    """
    from mcp.server.fastmcp.exceptions import ResourceError

    from vault_search import server as server_mod

    monkeypatch.setattr(server_mod, "_index", None)

    with pytest.raises(ResourceError):
        asyncio.run(server_mod.mcp.read_resource("schema://tools"))


def test_read_resource_unknown_uri_raises_error(
    vault_index: VaultIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未登録 URI (schema://unknown) で read_resource を呼ぶとエラーが送出される.

    FastMCP は登録なしの URI に対して ValueError("Unknown resource: ...") を送出する。
    """
    from vault_search import server as server_mod

    monkeypatch.setattr(server_mod, "_index", vault_index)

    with pytest.raises((ValueError, Exception)) as exc_info:
        asyncio.run(server_mod.mcp.read_resource("schema://unknown"))

    assert "unknown" in str(exc_info.value).lower() or "resource" in str(exc_info.value).lower(), (
        f"expected error message to mention resource or unknown, got: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
# Issue #38 / #179: schema://tools top-level metadata
# ---------------------------------------------------------------------------
#
# エージェントが初回接続時に overview / recommended_flow / errors を読むことで
# tool description 全体を context に入れずに済むようにする (B2.4)。
# #179 は FrontmatterKeyInfo の JSON Schema を公開して value_type の許容値を
# 機械検証可能にする (B4)。


def test_payload_has_version_key(vault_index: VaultIndex) -> None:
    """payload["version"] が module の _SCHEMA_VERSION 定数と一致すること.

    schema://tools resource payload 自身のバージョン (各 tool 契約のバージョン
    ではない)。agent が改変検知のために pin する対象。定数を直接 import して
    test 側にリテラルを置かないことで drift する唯一の SoT を resources.py に
    一本化する。
    """
    from vault_search.resources import _SCHEMA_VERSION, build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert payload.get("version") == _SCHEMA_VERSION, (
        f"payload['version']={payload.get('version')!r} vs _SCHEMA_VERSION={_SCHEMA_VERSION!r}"
    )


def test_payload_has_version_policy(vault_index: VaultIndex) -> None:
    """payload["version_policy"] が version の bumping semantics を説明すること (#193).

    agent が version を pin してキャッシュ戦略を立てる際、additive vs
    destructive 変更の区別を機械可読な文字列で取得できる必要がある。
    SemVer-like の「major/minor」か「additive/destructive」のいずれかの
    キーワードを含めることで agent が policy を読み取れるようにする。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert "version_policy" in payload, f"'version_policy' missing: keys={list(payload)}"
    policy = payload["version_policy"]
    assert isinstance(policy, str) and policy.strip(), (
        f"version_policy must be non-empty str, got: {policy!r}"
    )
    # additive vs destructive の概念を明示するキーワードが含まれること
    keywords = ("additive", "destructive", "major", "minor", "breaking")
    assert any(kw in policy.lower() for kw in keywords), (
        f"version_policy must mention bumping semantics (expected any of {keywords}): {policy!r}"
    )


def test_payload_version_matches_semver_like_format(vault_index: VaultIndex) -> None:
    """payload["version"] が semver-like (major.minor) 形式であること (#193).

    version_policy が SemVer 類似の semantics を宣言するため、version 文字列
    自体も最低限 `<major>.<minor>` の dotted numeric format に pin する。
    このテストは format のみ検証 (semantic correctness は別途 code review で保証)。
    """
    import re

    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    version = payload["version"]
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", version), (
        f"version must be semver-like (e.g. '2.0' or '2.0.0'), got: {version!r}"
    )


def test_payload_major_version_2_top_level_keys_pinned(vault_index: VaultIndex) -> None:
    """v2.x の payload top-level key set が既知のセットと一致すること (R2-2 enforcement).

    _VERSION_POLICY で "destructive changes bump major" と約束しているため、
    top-level key の削除や rename には major version の bump が必要。
    このテストはその約束を CI で部分的に強制する:
    - top-level key を削除した場合 → このテストが失敗して bump 漏れを検知
    - major が 3 に上がった場合 → このテストが失敗して内容更新を要求

    additive 変更 (key 追加) は _EXPECTED_V2_KEYS を更新すれば済む。
    minor version の bump は不要 (テストが通ったまま)。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    version = payload["version"]
    major = int(version.split(".")[0])
    assert major == 2, (
        f"This test pins the v2 top-level key set; update this test when bumping to v{major}.x "
        f"(current version: {version!r})"
    )

    expected_v2_keys = {
        "version",
        "version_policy",
        "overview",
        "recommended_flow",
        "errors",
        "tools",
        "frontmatter_key_info_schema",
        "frontmatter_keys",
    }
    actual_keys = set(payload.keys())
    removed = expected_v2_keys - actual_keys
    assert not removed, (
        f"Top-level keys removed without major version bump: {removed}. "
        f"Either bump _SCHEMA_VERSION to 3.x or restore the removed keys."
    )


def test_vault_search_in_recommended_flow(vault_index: VaultIndex) -> None:
    """recommended_flow に vault_search が含まれること (R2-1 guard).

    vault_search は "ほとんどのタスクの起点" として condition に明記されており、
    flow から消えると agent が主要な検索経路を見失う。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    flow = payload["recommended_flow"]
    flow_tools = [step["tool"] for step in flow]
    assert "vault_search" in flow_tools, (
        f"vault_search must be in recommended_flow (primary entry point); got tools: {flow_tools}"
    )


def test_payload_has_overview_with_entry_point_guidance(vault_index: VaultIndex) -> None:
    """payload["overview"] が初見 agent へのエントリ導線を案内すること.

    文字数検証は brittle なので、**agent が overview から読み取るべき contractual
    invariant** をテストする: 「schema://tools resource を入り口とすること」と
    「recommended_flow の参照」の **両方が必須** (AND 判定)。両キーワードが
    overview に揃って初めて agent が「自分が読んでいる resource 名」と「推奨
    呼出順序の所在」を一度に把握できる。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert "overview" in payload, f"'overview' key missing: keys={list(payload)}"
    overview = payload["overview"]
    assert isinstance(overview, str) and overview.strip(), (
        f"overview must be non-empty str, got: {overview!r}"
    )
    # 初回エントリ導線としての schema://tools 自身への言及
    assert "schema://tools" in overview, (
        f"overview should mention 'schema://tools' as entry point: {overview[:80]!r}..."
    )
    # 推奨 flow への参照 (recommended_flow key の存在を agent に伝えるため)
    assert "recommended_flow" in overview, (
        f"overview should reference 'recommended_flow' top-level key: {overview[:80]!r}..."
    )


def test_payload_has_recommended_flow_structure(vault_index: VaultIndex) -> None:
    """payload["recommended_flow"] が step/tool/optional キーを持つ list[dict].

    step は 1-based int で **連番かつ重複なし** (エージェントが "step 3 から"
    と言及しやすい可読性のため)。重複や 0 や 9 が混入していたら即検知する。

    #196 Option A に従い ``purpose`` フィールドは削除 (tool description との
    drift を構造的に排除)。#192 に従い ``optional: bool`` は必須フィールド、
    ``condition: str`` は optional=True の step で必須。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert "recommended_flow" in payload, f"'recommended_flow' missing: keys={list(payload)}"
    flow = payload["recommended_flow"]
    assert isinstance(flow, list) and len(flow) > 0, (
        f"recommended_flow must be non-empty list, got {flow!r}"
    )

    for idx, step in enumerate(flow, start=1):
        assert isinstance(step, dict), f"flow[{idx - 1}] must be dict: {step!r}"
        assert set(step.keys()) >= {"step", "tool", "optional"}, (
            f"flow[{idx - 1}] missing required keys: got {set(step.keys())}"
        )
        assert "purpose" not in step, (
            f"flow[{idx - 1}] should not have 'purpose' (removed per #196 Option A, "
            f"see tools[name].description instead): {step!r}"
        )
        assert isinstance(step["step"], int) and step["step"] >= 1, (
            f"step must be 1-based int, got {step['step']!r}"
        )
        assert isinstance(step["tool"], str) and step["tool"], (
            f"tool must be non-empty str, got {step['tool']!r}"
        )
        assert isinstance(step["optional"], bool), (
            f"optional must be bool, got {step['optional']!r}"
        )

    step_numbers = [step["step"] for step in flow]
    assert step_numbers == list(range(1, len(flow) + 1)), (
        f"step numbers must be consecutive 1-based ints with no gaps/duplicates, got {step_numbers}"
    )


def test_recommended_flow_optional_steps_have_condition(vault_index: VaultIndex) -> None:
    """optional=True の step は condition 文字列を持つこと (#192).

    agent が「このステップを踏むべきか」を機械可読に判定できるようにする。
    必須 step (optional=False) では condition は不要。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    flow = payload["recommended_flow"]

    optional_steps = [step for step in flow if step.get("optional") is True]
    assert optional_steps, (
        "at least one step should be optional (else agent will treat whole flow as mandatory)"
    )

    for step in optional_steps:
        assert "condition" in step, (
            f"optional step {step['step']} ({step['tool']}) must have 'condition': {step!r}"
        )
        assert isinstance(step["condition"], str) and step["condition"].strip(), (
            f"condition must be non-empty str, got {step['condition']!r}"
        )


def test_recommended_flow_tools_invocable(vault_index: VaultIndex) -> None:
    """recommended_flow の各 step.tool は実際に MCP 経由で呼出可能であること (#194).

    本質的に守りたい invariant は「flow に書いた tool が agent から実際に
    invoke 可能であること」で、`TOOL_SPECS` 登録 ≠ MCP server 登録の drift
    シナリオ (例: `@mcp.tool()` 登録漏れ) が TOOL_SPECS.keys() ベースの guard
    を素通りしてしまう。`server.mcp.list_tools()` の実登録結果を live 参照して
    drift の最終層を pin する。

    resource URI (schema://tools) は tool 名フィールドに混ぜず overview で触れる。
    """
    from vault_search import server as server_mod
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    flow = payload["recommended_flow"]
    flow_tools = {step["tool"] for step in flow}

    tools = asyncio.run(server_mod.mcp.list_tools())
    invocable = {t.name for t in tools}

    missing = flow_tools - invocable
    assert not missing, (
        f"recommended_flow references tools not invocable via MCP: {missing}\n"
        f"invocable (server.mcp.list_tools): {sorted(invocable)}"
    )


def test_payload_has_errors_section(vault_index: VaultIndex) -> None:
    """payload["errors"] が ErrorCode の全値をカバーし、各エントリに必須フィールドを持つこと (#191).

    error_code 単位 re-key 後も全 ErrorCode (VAULT_SEARCH_ERROR 含む) が
    description / raised_by / example を持つことを確認する。
    test_errors_covers_all_error_codes と合わせて exact-set + field-structure を両面保証。
    """
    from typing import get_args

    from vault_search.exceptions import ErrorCode
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert "errors" in payload, f"'errors' key missing: keys={list(payload)}"
    errors = payload["errors"]
    assert isinstance(errors, dict), f"errors must be dict, got {type(errors).__name__}"
    for code in get_args(ErrorCode):
        assert code in errors, f"errors['{code}'] missing: got {sorted(errors)}"
        entry = errors[code]
        assert isinstance(entry, dict), f"errors['{code}'] must be dict: {entry!r}"
        for field in ("description", "raised_by", "example"):
            assert field in entry, f"errors['{code}'] missing '{field}': {entry!r}"
            assert isinstance(entry[field], str) and entry[field].strip(), (
                f"errors['{code}']['{field}'] must be non-empty str: {entry[field]!r}"
            )


def test_errors_covers_all_error_codes(vault_index: VaultIndex) -> None:
    """errors が ErrorCode Literal の全値をカバーすること (#191 drift guard).

    `typing.get_args(ErrorCode)` の全要素が payload["errors"] キー集合と一致
    する。新 ErrorCode 追加時に _ERRORS 追加忘れを即検知する。
    """
    from typing import get_args

    from vault_search.exceptions import ErrorCode
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    errors = payload["errors"]

    declared_codes = set(get_args(ErrorCode))
    payload_codes = set(errors.keys())
    missing = declared_codes - payload_codes
    extra = payload_codes - declared_codes
    assert not missing, (
        f"ErrorCode values missing from payload['errors']: {missing}\n"
        f"declared in ErrorCode Literal but not documented"
    )
    assert not extra, (
        f"payload['errors'] has codes not in ErrorCode Literal: {extra}\n"
        f"either drifted key or missing from exceptions.ErrorCode"
    )


def test_errors_raised_by_matches_live_exception_class(vault_index: VaultIndex) -> None:
    """errors[code]['raised_by'] が実クラス名と一致すること (全 5 ErrorCode をカバー).

    #191 で error_code 単位に re-key された後も、どの Python 例外クラスから
    raise されるかを agent に伝える必要がある。class の __name__ を live 参照
    することで、exception class rename 時に即検知する。
    ValidationError サブコード (UNKNOWN_FRONTMATTER_KEY / UNSUPPORTED_RANGE_OPERATOR /
    VALIDATION_ERROR) はいずれも ValidationError から raise されることを pin する。
    """
    from vault_search.exceptions import NoteNotFoundError, VaultSearchError
    from vault_search.resources import build_schema_payload
    from vault_search.validation import ValidationError as VE

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    errors = payload["errors"]

    expected_raised_by = {
        "VAULT_SEARCH_ERROR": VaultSearchError.__name__,
        "NOTE_NOT_FOUND": NoteNotFoundError.__name__,
        "VALIDATION_ERROR": VE.__name__,
        "UNKNOWN_FRONTMATTER_KEY": VE.__name__,
        "UNSUPPORTED_RANGE_OPERATOR": VE.__name__,
    }
    for code, expected_cls_name in expected_raised_by.items():
        actual = errors[code]["raised_by"]
        assert actual == expected_cls_name, (
            f"errors['{code}']['raised_by'] drifted: "
            f"payload={actual!r} vs live={expected_cls_name!r}"
        )


def test_payload_has_frontmatter_key_info_schema(vault_index: VaultIndex) -> None:
    """payload に FrontmatterKeyInfo の JSON Schema が含まれること (#179).

    agent が value_type の許容値集合や sample_values の型を機械検証できるよう
    Pydantic model_json_schema() をそのまま公開する。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    assert "frontmatter_key_info_schema" in payload, (
        f"'frontmatter_key_info_schema' missing: keys={list(payload)}"
    )
    schema = payload["frontmatter_key_info_schema"]
    assert isinstance(schema, dict), f"schema must be dict: {type(schema).__name__}"
    assert schema.get("type") == "object", f"top-level must be object: {schema!r}"
    assert "properties" in schema, f"schema must have properties: {list(schema)}"
    for field in ("key", "value_type", "sample_values", "note_count"):
        assert field in schema["properties"], (
            f"FrontmatterKeyInfo field '{field}' missing from JSON Schema"
        )


def test_frontmatter_key_info_schema_pins_value_type_enum(vault_index: VaultIndex) -> None:
    """value_type プロパティの enum 集合が Literal 宣言値と一致すること.

    Pydantic v2 upgrade 等で Literal 展開挙動が変わった場合の regression guard。
    agent が value_type を比較するときの許容値集合を pin する。
    """
    from vault_search.resources import build_schema_payload

    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    schema = payload["frontmatter_key_info_schema"]
    value_type_schema = schema["properties"]["value_type"]

    enum = value_type_schema.get("enum")
    assert enum is not None, (
        f"value_type must expose enum for machine validation, got: {value_type_schema!r}"
    )
    expected = {"string", "number", "boolean", "array", "object", "mixed"}
    assert set(enum) == expected, (
        f"value_type enum drifted from FrontmatterKeyInfo Literal:\n"
        f"  got: {set(enum)}\n"
        f"  expected: {expected}"
    )


# NOTE: read_resource 経路の JSON round-trip 検証は
# `test_read_resource_schema_tools_returns_payload` (上方) が
# `payload == expected` の strict 等値で完全カバーしているため、新 top-level
# keys 専用の重複テストは設けない (C-R5)。新 keys は build_schema_payload に
# 追加された時点で expected も自動的に変わるため drift しない設計。
