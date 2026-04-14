"""統合シナリオテスト: schema://tools ドリブンのエージェント動作を再現する.

個別ユニット (schema resource / metadata_filter / fields / validation) は
各モジュールのテストで既にカバー済み。本ファイルは「AI エージェントが
schema resource を取得してから vault_search を呼ぶまで」の一連の流れを
スキーマ構造レベルで辿り、各機能の接合部が壊れていないことを確認する。

構造テスト方針:
- スキーマ文字列や順序には依存せず、キー/型の存在と意味のある部分集合で判定
- 既存 fixture (conftest.py の tmp_vault / vault_index) を使い、サンプル
  ノートの frontmatter に含まれる "status" / "priority" を利用する
"""

from __future__ import annotations

from typing import Any

import pytest

from vault_search import server as server_mod
from vault_search.indexer import VaultIndex
from vault_search.validation import ValidationError


# FastMCP Tool ラッパーから素の関数を取り出す (test_server.py と同じ流儀)
def _fn(tool: Any) -> Any:
    return getattr(tool, "fn", tool)


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
    """典型的なエージェント起動フローの統合テスト.

    1. schema://tools resource ハンドラを呼ぶ
    2. frontmatter_keys から有効なキー "status" を選ぶ
    3. vault_search の output_schema から SearchHit のフィールド "path"/"title" を取得
    4. それらを使って vault_search を呼ぶ
    5. 結果が SearchResponse で、各 SearchHit は指定したフィールドのみを持つ
    """
    # Step 1: schema resource を取得
    schema_handler = _fn(server_mod.schema_resource)
    payload = schema_handler()

    # payload の最低限の構造
    assert isinstance(payload, dict)
    assert "tools" in payload and "frontmatter_keys" in payload

    # Step 2: frontmatter_keys から "status" を取得 (Welcome.md / alpha.md に存在)
    frontmatter_keys = payload["frontmatter_keys"]
    assert "status" in frontmatter_keys, (
        f"'status' should be discovered in frontmatter_keys: {frontmatter_keys}"
    )
    filter_key = "status"
    filter_value = "active"  # Welcome.md と Research/alpha.md が持つ値

    # Step 3: vault_search の output_schema から SearchHit のフィールドを取得
    vault_search_entry = payload["tools"]["vault_search"]
    assert "output_schema" in vault_search_entry
    hit_props = _search_hit_properties(vault_search_entry["output_schema"])
    assert hit_props, "SearchHit properties must be discoverable from output_schema"

    # スキーマから "path" と "title" を「利用可能なフィールド」として採用
    assert "path" in hit_props and "title" in hit_props
    selected_fields = ["path", "title"]

    # Step 4: schema から得た情報だけで vault_search を呼ぶ
    search_fn = _fn(server_mod.vault_search)
    response = search_fn(
        "obsidian",  # Welcome.md / alpha.md の本文に含まれる
        None,  # tags
        None,  # folder
        20,  # limit
        0,  # offset
        selected_fields,  # fields
        {filter_key: filter_value},  # metadata_filter
    )

    # Step 5: 構造検証
    # fields 指定時は FastMCP の model_dump が default を補完しないよう plain dict を返す
    assert isinstance(response, dict)
    assert response["tier"] in (0, 1, 2)
    assert isinstance(response["total"], int)
    # status=active のノートが少なくとも 1 件ヒットすべき
    assert response["total"] >= 1, (
        f"expected at least one hit for status=active, got total={response['total']}"
    )
    assert len(response["results"]) >= 1

    for hit in response["results"]:
        assert isinstance(hit, dict)
        # fields で指定した 2 つのフィールドのみを持つ dict
        assert set(hit.keys()) == set(selected_fields), (
            f"expected only {selected_fields} in hit, got {sorted(hit.keys())}"
        )
        # path は string として意味のある値を持つ
        assert isinstance(hit["path"], str) and hit["path"].endswith(".md")
        assert isinstance(hit["title"], str)


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
