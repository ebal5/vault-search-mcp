"""統合シナリオテスト: schema://tools ドリブンのエージェント動作を再現する.

個別ユニット (schema resource / metadata_filter / fields / validation) は
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


def _resolve_sample_value(
    payload: dict[str, Any],
    key: str,
    vault_index: VaultIndex,
    *,
    restrict_paths: set[str] | None = None,
) -> Any:
    """``key`` に対する実在値を動的に取得する.

    - ``payload`` が value samples を公開していればそこから採用
    - 無ければ ``vault_index`` のフロントマターを走査
    - ``restrict_paths`` が渡されれば、そのパス集合に属するノートの
      フロントマターから値を取る (「特定クエリにヒットするノートが
      実際に持っている値」を得るため)

    固定値 ``"active"`` 等の文字列リテラルに依存しないので、schema と
    実データの drift を確実に検知する。
    """
    samples = payload.get("frontmatter_value_samples") or {}
    bucket = samples.get(key)
    if isinstance(bucket, list) and bucket:
        return bucket[0]
    for note in vault_index.recent_notes(limit=100):
        if restrict_paths is not None and note["path"] not in restrict_paths:
            continue
        detail = vault_index.get_note(note["path"])
        if detail is None:
            continue
        fm = detail.get("frontmatter") or {}
        if key in fm:
            value = fm[key]
            if isinstance(value, list) and value:
                return value[0]
            if isinstance(value, (str, int, float, bool)):
                return value
    return None


def _call_vault_search_via_mcp(arguments: dict[str, Any]) -> dict[str, Any]:
    """FastMCP の ``call_tool`` 経路で vault_search を呼び structured 結果を返す.

    ``call_tool`` は ``(content_blocks, structured)`` の tuple を返す。
    tool 戻り型を dict に統一したため (FastMCP の wrap_output 回避)、
    structured はもはや ``{"result": ...}`` で包まれず、トップで
    tier/total/results を直接持つ。
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
    """vault_search の output_schema から SearchHit 相当の properties を取り出す.

    SearchResponse の JSON Schema は ``$defs`` に SearchHit を抱えるので、
    path + title + snippet を全て持つ properties dict を探して返す。
    """
    defs = output_schema.get("$defs") or output_schema.get("definitions") or {}
    for d in defs.values():
        if isinstance(d, dict) and "properties" in d:
            props = d["properties"]
            if {"path", "title", "snippet"}.issubset(props.keys()):
                return props
    # fallback: ルート直下 (list wrap 等) に SearchHit があった場合
    if "properties" in output_schema:
        props = output_schema["properties"]
        if {"path", "title", "snippet"}.issubset(props.keys()):
            return props
    return {}


@pytest.fixture(autouse=True)
def _inject_index(vault_index: VaultIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    """server モジュールの _index をテスト用に差し替え."""
    monkeypatch.setattr(server_mod, "_index", vault_index)


# ---------------------------------------------------------------------------
# Scenario A: schema -> vault_search (metadata_filter + fields)
# ---------------------------------------------------------------------------


def test_schema_driven_agent_flow(vault_index: VaultIndex) -> None:
    """典型的なエージェント起動フローの統合テスト (MCP protocol 経路).

    1. ``mcp.read_resource('schema://tools')`` で schema を取得
    2. ``frontmatter_keys`` から実在するキーを **動的抽出** (固定値に依存しない)
    3. schema の ``frontmatter_value_samples`` から、そのキーの **実在する値** を抽出
    4. ``output_schema`` から SearchHit の **実在するフィールド** を抽出
    5. 抽出した値で ``mcp.call_tool('vault_search', ...)`` を呼ぶ
    6. structured 出力と text content の両方が整合し、fields 指定どおりに
       絞られていることを確認
    """
    # Step 1: schema resource を FastMCP 経路で取得
    payload = _read_schema_via_mcp()
    assert "tools" in payload and "frontmatter_keys" in payload

    # Step 2: frontmatter_keys から動的に 1 つ選ぶ (固定値 "status" 依存を避ける)
    frontmatter_keys = payload["frontmatter_keys"]
    assert isinstance(frontmatter_keys, list) and frontmatter_keys, (
        f"frontmatter_keys must be a non-empty list, got {frontmatter_keys!r}"
    )
    # "status" が schema にあればそれを使う (サンプルの expectation と一致させる)。
    # 無ければ最初のキーで代替。これで schema と実データが drift すれば必ず失敗する
    filter_key = "status" if "status" in frontmatter_keys else frontmatter_keys[0]

    # Step 3: クエリにヒットするノートに絞って、filter_key の実在値を動的取得
    # (「query でヒットするノートが実際に持っている値」を採ることで、
    #  filter 適用後も最低 1 件残ることを保証する)
    query = "obsidian"
    prelim = _call_vault_search_via_mcp({"query": query, "limit": 100})
    assert prelim["total"] >= 1, f"baseline query '{query}' must hit at least one note in fixture"
    hit_paths = {r["path"] for r in prelim["results"]}
    filter_value = _resolve_sample_value(payload, filter_key, vault_index, restrict_paths=hit_paths)
    assert filter_value is not None, (
        f"cannot find any sample value for key '{filter_key}' "
        f"among notes hitting query '{query}' (paths={sorted(hit_paths)})"
    )

    # Step 4: vault_search の output_schema から SearchHit のフィールドを取得
    vault_search_entry = payload["tools"]["vault_search"]
    assert "output_schema" in vault_search_entry
    hit_props = _search_hit_properties(vault_search_entry["output_schema"])
    assert hit_props, "SearchHit properties must be discoverable from output_schema"

    # path と title は SearchHit の最低限のフィールドとして schema に必ず存在
    assert "path" in hit_props and "title" in hit_props, (
        f"SearchHit schema must contain 'path' and 'title': {sorted(hit_props)}"
    )
    # スキーマに含まれるフィールドから 2 つを動的選択 (固定リスト依存を避ける)
    selected_fields = [f for f in ("path", "title") if f in hit_props]
    assert len(selected_fields) == 2

    # Step 5: MCP protocol 経由で vault_search を呼び出す
    result = _call_vault_search_via_mcp(
        {
            "query": query,
            "fields": selected_fields,
            "metadata_filter": {filter_key: filter_value},
        }
    )

    # Step 6: structured 出力の検証
    assert isinstance(result, dict)
    assert result["tier"] in (0, 1, 2)
    assert isinstance(result["total"], int)
    # schema から取った実在の値でフィルタした結果、最低 1 件はヒットする
    assert result["total"] >= 1, (
        f"expected >=1 hit for {filter_key}={filter_value!r}, got total={result['total']}"
    )
    assert len(result["results"]) >= 1

    for hit in result["results"]:
        assert isinstance(hit, dict)
        # fields で指定した 2 つのフィールドのみを持つ dict
        assert set(hit.keys()) == set(selected_fields), (
            f"expected only {selected_fields} in hit, got {sorted(hit.keys())}"
        )
        assert isinstance(hit["path"], str) and hit["path"].endswith(".md")
        assert isinstance(hit["title"], str)


def test_mcp_call_tool_returns_full_schema_without_fields(
    vault_index: VaultIndex,
) -> None:
    """fields を指定しない MCP 呼び出しは SearchHit 全フィールドを返す.

    ``fields`` の「指定時のみ subset」挙動が、未指定ケースで regression
    していないことを MCP 経路で確認する。
    """
    payload = _read_schema_via_mcp()
    hit_props = _search_hit_properties(payload["tools"]["vault_search"]["output_schema"])
    assert hit_props

    result = _call_vault_search_via_mcp({"query": "obsidian"})
    assert result["total"] >= 1
    for hit in result["results"]:
        # schema が公開する SearchHit プロパティを全て含むこと
        # (FastMCP が default 値で補完する可能性も考慮して subset 比較)
        assert set(hit_props).issubset(hit.keys()), (
            f"missing keys: {set(hit_props) - set(hit.keys())}"
        )


# ---------------------------------------------------------------------------
# Scenario B: 不正入力は ValidationError で弾かれる
# ---------------------------------------------------------------------------


def test_invalid_field_and_metadata_filter_both_rejected(vault_index: VaultIndex) -> None:
    """fields の不正名 + metadata_filter の不正キーを同時に渡すと ValidationError.

    どちらが先にチェックされるかは実装依存でよいが、エラーは必ず出る。
    実装の順序変更に耐える構造テスト。
    """
    search_fn = _fn(server_mod.vault_search)
    with pytest.raises(ValidationError):
        search_fn(
            "obsidian",
            None,
            None,
            20,
            0,
            ["no_such_field"],  # 不正な field 名
            {"bad key!": "x"},  # 不正な frontmatter キー (スペース/`!` は識別子外)
        )


def test_invalid_field_alone_rejected(vault_index: VaultIndex) -> None:
    """fields のみ不正な場合でも ValidationError."""
    search_fn = _fn(server_mod.vault_search)
    with pytest.raises(ValidationError):
        search_fn("obsidian", None, None, 20, 0, ["no_such_field"], None)


def test_invalid_metadata_filter_alone_rejected(vault_index: VaultIndex) -> None:
    """metadata_filter のみ不正な場合でも ValidationError."""
    search_fn = _fn(server_mod.vault_search)
    with pytest.raises(ValidationError):
        search_fn("obsidian", None, None, 20, 0, None, {"bad key!": "x"})
