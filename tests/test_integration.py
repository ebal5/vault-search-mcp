"""統合シナリオテスト: schema://tools ドリブンのエージェント動作を再現する.

個別ユニット (schema resource / metadata_filter / validation) は
各モジュールのテストで既にカバー済み。本ファイルは「AI エージェントが
schema resource を取得してから vault_search を呼ぶまで」の一連の流れを
スキーマ構造レベルで辿り、各機能の接合部が壊れていないことを確認する。

構造テスト方針:
- スキーマ文字列や順序には依存せず、キー/型の存在と意味のある部分集合で判定
- 既存 fixture (conftest.py の tmp_vault / vault_index) を使い、サンプル
  ノートの frontmatter に含まれる "status" / "priority" を利用する
- 可能な限り FastMCP の実 async API (``read_resource`` / ``call_tool``) を
  通し、Python 関数直接呼び出しを避ける (MCP protocol 経路の regression を
  検知するため)

Scenario A は 6 つの独立プロパティテストに分割している:
1. test_schema_resource_reachable     — schema リソースの取得可否
2. test_frontmatter_keys_match_vault  — frontmatter_keys と実 vault の一致
3. test_value_samples_subset          — value_samples が実値の部分集合
4. test_output_schema_extractable     — outputSchema の抽出可否
5. test_mcp_fields_match_model        — SearchHit フィールドと Pydantic モデルの一致
6. test_text_structured_consistency   — text / structured 出力の一致
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from vault_search import server as server_mod
from vault_search.indexer import VaultIndex
from vault_search.validation import ValidationError


# FastMCP Tool ラッパーから素の関数を取り出す (test_server.py と同じ流儀)
def _fn(tool: Any) -> Any:
    return getattr(tool, "fn", tool)


def _read_schema_via_mcp() -> dict[str, Any]:
    """FastMCP の ``read_resource`` 経路で schema://tools を取得して JSON パース.

    ``read_resource`` は ``list[ReadResourceContents]`` を返し、各要素の
    ``content`` はリソース関数の返値が JSON シリアライズされた文字列である。
    """
    contents = asyncio.run(server_mod.mcp.read_resource("schema://tools"))
    assert contents, "schema://tools resource returned empty"
    payload = json.loads(contents[0].content)
    assert isinstance(payload, dict)
    return payload


def _call_vault_search_via_mcp(arguments: dict[str, Any]) -> dict[str, Any]:
    """FastMCP の ``call_tool`` 経路で vault_search を呼び structured 結果を返す.

    ``call_tool`` は ``(content_blocks, structured)`` の tuple を返す。
    tool 戻り型を dict に統一したため (FastMCP の wrap_output 回避)、
    structured はもはや ``{"result": ...}`` で包まれず、トップで
    tier/total/results を直接持つ。

    text content == structured content の assertion を常に行う。
    """
    content, structured = asyncio.run(server_mod.mcp.call_tool("vault_search", arguments))
    # MCP の Text content も JSON として妥当であることを検証
    assert content, "call_tool returned no content blocks"
    parsed_text = json.loads(content[0].text)
    # structured と text の間で一貫性を確認
    assert structured == parsed_text, "structured output と text content の内容が一致しない"
    assert "tier" in structured, (
        f"structured content should not be wrapped, got keys={list(structured.keys())}"
    )
    return structured


def _search_hit_properties(output_schema: dict[str, Any]) -> dict[str, Any]:
    """vault_search の output_schema から SearchHit の properties を取り出す.

    SearchResponse は ``results: list[SearchHit]`` を持つため Pydantic は
    ``$defs.SearchHit`` に SearchHit を切り出し ``results.items`` を ``$ref``
    で参照する。直接パスで取り出せばよい (PR #92 で fields 削除後、
    anyOf / oneOf ブランチは発生しない)。
    """
    return output_schema["$defs"]["SearchHit"]["properties"]


@pytest.fixture(autouse=True)
def _inject_index(vault_index: VaultIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    """server モジュールの _index をテスト用に差し替え."""
    monkeypatch.setattr(server_mod, "_index", vault_index)


@pytest.fixture
def schema_payload() -> dict[str, Any]:
    """schema://tools を FastMCP 経路で取得したペイロード.

    ``_inject_index`` autouse fixture が先行して server_mod._index を
    セットするため、read_resource 呼び出し時に index が利用可能である。
    """
    return _read_schema_via_mcp()


# ---------------------------------------------------------------------------
# Scenario A: schema -> vault_search (metadata_filter) — 6 独立プロパティ
# ---------------------------------------------------------------------------


def test_schema_resource_reachable(schema_payload: dict[str, Any]) -> None:
    """schema://tools リソースが取得可能で必須トップレベルキーを含む."""
    assert "tools" in schema_payload
    assert "frontmatter_keys" in schema_payload


def test_frontmatter_keys_match_vault(
    schema_payload: dict[str, Any], vault_index: VaultIndex
) -> None:
    """schema の frontmatter_keys が実 vault のフロントマターキーと一致する."""
    frontmatter_keys = schema_payload["frontmatter_keys"]
    assert isinstance(frontmatter_keys, list) and frontmatter_keys, (
        f"frontmatter_keys must be a non-empty list, got {frontmatter_keys!r}"
    )
    # frontmatter_keys は list[dict] (FrontmatterKeyInfo の model_dump 結果)
    assert all(isinstance(k, dict) for k in frontmatter_keys), (
        "frontmatter_keys の各要素は dict (FrontmatterKeyInfo) であるべき"
    )
    all_fm_keys: set[str] = set()
    for note in vault_index.recent_notes(limit=100):
        detail = vault_index.get_note(note["path"])
        if detail:
            all_fm_keys.update((detail.get("frontmatter") or {}).keys())
    for info in frontmatter_keys:
        key = info["key"]
        assert key in all_fm_keys, (
            f"schema key {key!r} not found in any vault note (vault keys: {sorted(all_fm_keys)})"
        )


def test_value_samples_subset(schema_payload: dict[str, Any], vault_index: VaultIndex) -> None:
    """frontmatter_keys[*].sample_values の各値が実 vault の frontmatter 値と整合する.

    正規化 (parser._normalize_scalar) 後の文字列表現と sample_values 値を比較:
      - scalar (bool/int/float/date) は isoformat / str() で文字列化されている
      - 配列値は json.dumps(..., ensure_ascii=False) の文字列
      - value_type='object' の親キーは sample_values=[] なので skip
    """
    fm_keys = schema_payload.get("frontmatter_keys") or []
    assert isinstance(fm_keys, list), "frontmatter_keys must be a list"
    assert len(fm_keys) > 0, "fixture vault should have frontmatter keys"

    # vault から各 key の正規化済み値の集合を収集
    expected_by_key: dict[str, set[str]] = {}
    for note in vault_index.recent_notes(limit=100):
        detail = vault_index.get_note(note["path"])
        if detail is None:
            continue
        fm = detail.get("frontmatter") or {}
        for k, v in fm.items():
            if v is None or isinstance(v, dict):
                continue
            bucket = expected_by_key.setdefault(k, set())
            if isinstance(v, list):
                bucket.add(json.dumps(v, ensure_ascii=False))
            elif isinstance(v, str):
                if v.strip():
                    bucket.add(v)
            else:
                bucket.add(str(v))

    for info in fm_keys:
        if info.get("value_type") == "object":
            assert info["sample_values"] == [], (
                f"object 型 '{info['key']}' の sample_values は [] でなければならない"
            )
            continue
        expected = expected_by_key.get(info["key"], set())
        for sample_val in info["sample_values"]:
            assert sample_val in expected, (
                f"sample value {sample_val!r} for key {info['key']!r} "
                f"not found in vault (expected subset: {sorted(expected)})"
            )


def test_output_schema_extractable(schema_payload: dict[str, Any]) -> None:
    """vault_search の outputSchema が schema リソースから抽出できる."""
    vault_search_entry = schema_payload["tools"]["vault_search"]
    assert "output_schema" in vault_search_entry, "vault_search entry missing output_schema"
    output_schema = vault_search_entry["output_schema"]
    assert "$defs" in output_schema, "output_schema missing $defs"
    assert "SearchHit" in output_schema["$defs"], "output_schema $defs missing SearchHit"
    assert "properties" in output_schema["$defs"]["SearchHit"], "SearchHit missing properties"


def test_mcp_fields_match_model(schema_payload: dict[str, Any]) -> None:
    """SearchHit の schema フィールドが Pydantic モデル定義の全フィールドを含む."""
    expected_hit_fields = {
        "path",
        "title",
        "folder",
        "tags",
        "snippet",
        "score",
        "created_at",
        "modified_at",
    }
    vault_search_entry = schema_payload["tools"]["vault_search"]
    hit_props = _search_hit_properties(vault_search_entry["output_schema"])
    assert expected_hit_fields.issubset(hit_props.keys()), (
        f"SearchHit schema missing fields: {expected_hit_fields - hit_props.keys()}"
    )


def test_text_structured_consistency(vault_index: VaultIndex) -> None:
    """vault_search の MCP text content と structured content が一致する."""
    result = _call_vault_search_via_mcp({"query": "obsidian", "limit": 10})
    # _call_vault_search_via_mcp 内で text == structured の assertion 済み
    # 追加: structured が wrap されていない正しい形であることを確認
    assert isinstance(result["total"], int)
    assert isinstance(result["results"], list)


# ---------------------------------------------------------------------------
# Scenario B: 不正入力は ValidationError で弾かれる
# ---------------------------------------------------------------------------


def test_invalid_metadata_filter_alone_rejected(vault_index: VaultIndex) -> None:
    """metadata_filter の不正キーは ValidationError."""
    search_fn = _fn(server_mod.vault_search)
    with pytest.raises(ValidationError):
        search_fn("obsidian", None, None, 20, 0, {"bad key!": "x"})
