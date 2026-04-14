"""server.py のテスト: 7 ツールの happy path + エラー."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vault_search import server as server_mod
from vault_search.indexer import VaultIndex
from vault_search.schemas import (
    FolderCount,
    NoteDetail,
    NoteNotFoundError,
    RecentNote,
    SearchResponse,
    TagCount,
)


@pytest.fixture(autouse=True)
def _inject_index(vault_index: VaultIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    """モジュールグローバルの _index にテスト用インデックスをセット."""
    monkeypatch.setattr(server_mod, "_index", vault_index)


# ツール関数は FastMCP でラップされているため .fn 属性で素の関数を取得
def _fn(tool):
    # FastMCP Tool オブジェクトは .fn に元関数を保持。
    # 版差異に備え getattr フォールバック。
    return getattr(tool, "fn", tool)


def test_get_index_raises_when_uninitialized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "_index", None)
    with pytest.raises(RuntimeError):
        server_mod._get_index()


# ---------------------------------------------------------------------------
# Indexer 層の dict インタフェース (server が Pydantic にラップする前の生データ)
# ---------------------------------------------------------------------------


def test_vault_search_happy(vault_index: VaultIndex) -> None:
    res = server_mod._get_index().search("obsidian")
    assert set(res.keys()) >= {"tier", "total", "results"}
    assert isinstance(res["results"], list)


def test_vault_search_empty_query(vault_index: VaultIndex) -> None:
    res = server_mod._get_index().search("")
    assert res["results"] == []


def test_vault_get_note_happy(vault_index: VaultIndex) -> None:
    # server.vault_get_note の振る舞いを直接検証
    idx = server_mod._get_index()
    note = idx.get_note("Welcome.md")
    assert note is not None
    assert note["title"] == "Welcome"


def test_vault_get_note_missing_returns_none() -> None:
    """indexer 層では見つからないとき None を返す (server 層で NoteNotFoundError に変換)."""
    idx = server_mod._get_index()
    result = idx.get_note("does-not-exist.md")
    assert result is None


def test_vault_recent_happy(vault_index: VaultIndex) -> None:
    notes = server_mod._get_index().recent_notes(limit=5)
    assert isinstance(notes, list)
    if notes:
        assert "path" in notes[0]


def test_vault_tags_happy(vault_index: VaultIndex) -> None:
    tags = server_mod._get_index().list_tags()
    assert isinstance(tags, list)
    if tags:
        assert set(tags[0].keys()) == {"tag", "count"}


def test_vault_folders_happy(vault_index: VaultIndex) -> None:
    folders = server_mod._get_index().list_folders()
    assert isinstance(folders, list)
    if folders:
        assert set(folders[0].keys()) == {"folder", "count"}


def test_vault_reindex_happy(vault_index: VaultIndex) -> None:
    stats = server_mod._get_index().build_index()
    assert set(stats.keys()) >= {"added", "updated", "deleted", "skipped", "errors"}


def test_vault_stats_happy(vault_index: VaultIndex) -> None:
    s = server_mod._get_index().stats()
    assert "total_notes" in s
    assert s["total_notes"] > 0


# ---------------------------------------------------------------------------
# FastMCP tool wrapper smoke tests — 実際の @mcp.tool() 関数を直に呼ぶ
# ツール関数は Pydantic モデルを返すようになった (PR #2)。
# ---------------------------------------------------------------------------


def test_mcp_tool_vault_search(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_search)
    res = fn("obsidian")
    # tool は常に plain dict を返す (FastMCP の wrap_output 回避のため)
    assert isinstance(res, dict)
    assert res["tier"] in (0, 1, 2)
    assert isinstance(res["total"], int)
    assert isinstance(res["results"], list)
    # 参考: SearchResponse として再構築可能な形を維持していること
    SearchResponse.model_validate(res)


def test_mcp_tool_vault_get_note_missing(vault_index: VaultIndex) -> None:
    """存在しないパスでは NoteNotFoundError を送出する (旧 error dict から変更)."""
    fn = _fn(server_mod.vault_get_note)
    with pytest.raises(NoteNotFoundError) as exc_info:
        fn("no/such/note.md")
    assert exc_info.value.path == "no/such/note.md"


def test_mcp_tool_vault_get_note_found(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_get_note)
    res = fn("Welcome.md")
    assert isinstance(res, dict)
    assert res["title"] == "Welcome"
    assert res["path"] == "Welcome.md"
    # NoteDetail として再構築可能
    NoteDetail.model_validate(res)


def test_mcp_tool_vault_recent(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_recent)
    res = fn(5, None)
    # envelope dict `{"notes": [...]}` を返す (FastMCP の list wrap 回避のため)
    assert isinstance(res, dict)
    assert set(res.keys()) == {"notes"}
    assert isinstance(res["notes"], list)
    for item in res["notes"]:
        assert isinstance(item, dict)
        RecentNote.model_validate(item)


def test_mcp_tool_vault_tags(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_tags)
    res = fn()
    assert isinstance(res, dict)
    assert set(res.keys()) == {"tags"}
    assert isinstance(res["tags"], list)
    for item in res["tags"]:
        assert isinstance(item, dict)
        TagCount.model_validate(item)


def test_mcp_tool_vault_folders(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_folders)
    res = fn()
    assert isinstance(res, dict)
    assert set(res.keys()) == {"folders"}
    assert isinstance(res["folders"], list)
    for item in res["folders"]:
        assert isinstance(item, dict)
        FolderCount.model_validate(item)


def test_mcp_tool_vault_folders_root_is_empty_string(vault_index: VaultIndex) -> None:
    """vault_folders の root 直下は '' に統一され '(root)' sentinel は出ない."""
    fn = _fn(server_mod.vault_folders)
    res = fn()
    folders = {item["folder"] for item in res["folders"]}
    assert "" in folders
    assert "(root)" not in folders


def test_mcp_tool_vault_reindex(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_reindex)
    res = fn(False)
    assert isinstance(res, dict)
    # added/updated/deleted/skipped/errors を必須キーで持つ flat JSON
    for key in ("added", "updated", "deleted", "skipped", "errors"):
        assert res[key] >= 0


def test_mcp_tool_vault_stats(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_stats)
    res = fn()
    assert isinstance(res, dict)
    assert res["total_notes"] > 0
    assert res["db_size_bytes"] >= 0
    assert res["vault_root"]  # non-empty path string


# ---------------------------------------------------------------------------
# fields parameter (Issue #9) — AI エージェントが返却フィールドを絞って
# context window を節約するための引数。Red フェーズ: 実装前の失敗テスト。
# ---------------------------------------------------------------------------


def test_vault_search_fields_subset(vault_index: VaultIndex) -> None:
    """fields=["path", "title"] で subset 返却 (tool 関数直接呼び出し)."""
    fn = _fn(server_mod.vault_search)
    res = fn("obsidian", None, None, 20, 0, ["path", "title"])
    # fields 指定時は plain dict を返す (FastMCP の output_model.model_dump 経路を bypass)
    assert isinstance(res, dict)
    assert set(res.keys()) >= {"tier", "total", "results"}
    assert isinstance(res["results"], list)
    assert len(res["results"]) > 0
    for hit in res["results"]:
        assert isinstance(hit, dict)
        assert set(hit.keys()) == {"path", "title"}
        assert hit["path"] != ""
        assert isinstance(hit["title"], str)


def test_vault_search_fields_none_returns_all(vault_index: VaultIndex) -> None:
    """fields=None (デフォルト) は全フィールド返却で後方互換."""
    fn = _fn(server_mod.vault_search)
    res = fn("obsidian", None, None, 20, 0, None)
    assert isinstance(res, dict)
    assert len(res["results"]) > 0
    # 通常の検索結果には snippet が載るはず (3文字以上クエリ)
    hit = res["results"][0]
    # 全キーが存在
    assert set(hit.keys()) >= {
        "path",
        "title",
        "folder",
        "tags",
        "snippet",
        "score",
        "created_at",
        "modified_at",
    }


def test_vault_search_fields_empty_raises(vault_index: VaultIndex) -> None:
    """fields=[] は ValueError."""
    fn = _fn(server_mod.vault_search)
    with pytest.raises(ValueError):
        fn("obsidian", None, None, 20, 0, [])


def test_vault_search_fields_nonexistent_raises(vault_index: VaultIndex) -> None:
    """fields=["nonexistent"] は ValueError."""
    fn = _fn(server_mod.vault_search)
    with pytest.raises(ValueError):
        fn("obsidian", None, None, 20, 0, ["nonexistent"])


def test_vault_search_fields_mixed_valid_invalid_raises(vault_index: VaultIndex) -> None:
    """有効 + 無効 混在でも厳格に ValueError."""
    fn = _fn(server_mod.vault_search)
    with pytest.raises(ValueError):
        fn("obsidian", None, None, 20, 0, ["path", "bogus"])


def test_vault_get_note_fields_subset(vault_index: VaultIndex) -> None:
    """fields=["path", "title"] で subset 返却 (tool 関数直接呼び出し)."""
    fn = _fn(server_mod.vault_get_note)
    res = fn("Welcome.md", ["path", "title"])
    # fields 指定時は plain dict
    assert isinstance(res, dict)
    assert set(res.keys()) == {"path", "title"}
    assert res["path"] == "Welcome.md"
    assert res["title"] == "Welcome"


def test_vault_get_note_fields_empty_raises(vault_index: VaultIndex) -> None:
    """fields=[] は ValueError."""
    fn = _fn(server_mod.vault_get_note)
    with pytest.raises(ValueError):
        fn("Welcome.md", [])


def test_vault_get_note_fields_nonexistent_raises(vault_index: VaultIndex) -> None:
    """fields=["bogus"] は ValueError."""
    fn = _fn(server_mod.vault_get_note)
    with pytest.raises(ValueError):
        fn("Welcome.md", ["bogus"])


def test_vault_recent_fields_subset(vault_index: VaultIndex) -> None:
    """fields=["path"] で subset 返却 (envelope dict `{"notes": [...]}`)."""
    fn = _fn(server_mod.vault_recent)
    res = fn(5, None, ["path"])
    assert isinstance(res, dict)
    assert set(res.keys()) == {"notes"}
    notes = res["notes"]
    assert isinstance(notes, list)
    assert len(notes) > 0
    for item in notes:
        assert isinstance(item, dict)
        assert set(item.keys()) == {"path"}
        assert item["path"] != ""


def test_vault_recent_fields_empty_raises(vault_index: VaultIndex) -> None:
    """fields=[] は ValueError."""
    fn = _fn(server_mod.vault_recent)
    with pytest.raises(ValueError):
        fn(5, None, [])


def test_vault_recent_fields_nonexistent_raises(vault_index: VaultIndex) -> None:
    """fields=["nope"] は ValueError."""
    fn = _fn(server_mod.vault_recent)
    with pytest.raises(ValueError):
        fn(5, None, ["nope"])


# ---------------------------------------------------------------------------
# metadata_filter (Issue #5) — MCP tool 経由の振る舞いを検証する Red テスト。
# ---------------------------------------------------------------------------


def test_mcp_tool_vault_search_metadata_filter_eq(vault_index: VaultIndex) -> None:
    """eq 暗黙の metadata_filter で絞り込み."""
    fn = _fn(server_mod.vault_search)
    res = fn("obsidian", None, None, 20, 0, None, {"status": "active"})
    assert isinstance(res, dict)
    paths = {hit["path"] for hit in res["results"]}
    assert "Welcome.md" in paths
    assert "Projects/日本語ノート.md" not in paths


def test_mcp_tool_vault_search_metadata_filter_in(vault_index: VaultIndex) -> None:
    """in 演算子の metadata_filter で絞り込み."""
    fn = _fn(server_mod.vault_search)
    res = fn(
        "obsidian",
        None,
        None,
        20,
        0,
        None,
        {"priority": {"in": ["high", "low"]}},
    )
    assert isinstance(res, dict)
    paths = {hit["path"] for hit in res["results"]}
    assert "Welcome.md" in paths
    assert "Research/alpha.md" in paths
    assert "Projects/日本語ノート.md" not in paths


def test_mcp_tool_vault_search_metadata_filter_invalid_operator(
    vault_index: VaultIndex,
) -> None:
    """未サポート演算子は ValueError (FastMCP エラーレスポンス)."""
    fn = _fn(server_mod.vault_search)
    with pytest.raises(ValueError):
        fn("obsidian", None, None, 20, 0, None, {"x": {"regex": "foo"}})


# ---------------------------------------------------------------------------
# fields parameter — FastMCP の convert_result 経路で実際にレスポンス JSON が
# 指定キーのみに絞られているかを検証する。tool 関数を直接呼んだ結果を
# model_dump すると exclude_unset が効かない経路で全キーが返るため、
# これらのテストは bug 修正前は必ず失敗する。
# ---------------------------------------------------------------------------


def _call_tool_structured(tool_name: str, arguments: dict) -> Any:
    """FastMCP の経路 (convert_result=True) でツールを実行し structured content を返す.

    union 戻り型のツールは FastMCP が自動で ``{"result": ...}`` にラップするため、
    ``result`` キーがあれば内側を返す (MCP プロトコル上も clients は通常
    unstructured TextContent を読むので実害はないが、テスト側で unwrap する)。
    """
    mgr = server_mod.mcp._tool_manager
    tool = mgr.get_tool(tool_name)
    assert tool is not None, f"tool not registered: {tool_name}"
    result = asyncio.run(tool.run(arguments, convert_result=True))
    # output_schema が定義されていれば (unstructured, structured) のタプル。
    assert isinstance(result, tuple), f"expected structured output tuple, got {type(result)}"
    _unstructured, structured = result
    if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
        return structured["result"]
    return structured


def test_vault_search_fields_actually_subsets_response(vault_index: VaultIndex) -> None:
    """MCP 経路で fields 指定外のキーがレスポンスから除外される."""
    structured = _call_tool_structured(
        "vault_search",
        {"query": "obsidian", "fields": ["path", "title"], "limit": 5},
    )
    assert "results" in structured
    assert len(structured["results"]) > 0
    for hit in structured["results"]:
        assert set(hit.keys()) == {"path", "title"}, f"unexpected keys: {hit.keys()}"
        assert "snippet" not in hit
        assert "tags" not in hit
        assert "score" not in hit


def test_vault_get_note_fields_actually_subsets_response(vault_index: VaultIndex) -> None:
    """vault_get_note も同様に指定キーのみ返す."""
    structured = _call_tool_structured(
        "vault_get_note",
        {"path": "Welcome.md", "fields": ["path", "title"]},
    )
    assert set(structured.keys()) == {"path", "title"}, f"unexpected keys: {structured.keys()}"
    assert "content" not in structured
    assert "frontmatter" not in structured


def test_vault_recent_fields_actually_subsets_response(vault_index: VaultIndex) -> None:
    """vault_recent 各要素も同様 (envelope dict 経由)."""
    structured = _call_tool_structured(
        "vault_recent",
        {"limit": 5, "fields": ["path"]},
    )
    assert isinstance(structured, dict)
    assert "notes" in structured
    items = structured["notes"]
    assert isinstance(items, list)
    assert len(items) > 0
    for item in items:
        assert set(item.keys()) == {"path"}, f"unexpected keys: {item.keys()}"
        assert "title" not in item
        assert "modified_at" not in item


def test_vault_search_fields_none_mcp_returns_all(vault_index: VaultIndex) -> None:
    """fields=None では MCP 経路で全フィールドが返る (後方互換)."""
    structured = _call_tool_structured(
        "vault_search",
        {"query": "obsidian", "limit": 5},
    )
    assert len(structured["results"]) > 0
    hit = structured["results"][0]
    assert set(hit.keys()) >= {
        "path",
        "title",
        "folder",
        "tags",
        "snippet",
        "score",
        "created_at",
        "modified_at",
    }


# ---------------------------------------------------------------------------
# Issue #51 R3.4 + Issue #61 R9.1: limit / offset 境界値 validation と
# vault_recent の offset ページング対応
# ---------------------------------------------------------------------------


def test_vault_search_rejects_negative_limit(vault_index: VaultIndex) -> None:
    """limit が負値のとき ValidationError (ValueError 派生) を投げる."""
    from vault_search.validation import ValidationError

    fn = _fn(server_mod.vault_search)
    with pytest.raises((ValueError, ValidationError)):
        fn("obsidian", limit=-1)


def test_vault_search_rejects_zero_limit(vault_index: VaultIndex) -> None:
    """limit=0 は ValidationError。意味のない呼び出しを早期に弾く."""
    from vault_search.validation import ValidationError

    fn = _fn(server_mod.vault_search)
    with pytest.raises((ValueError, ValidationError)):
        fn("obsidian", limit=0)


def test_vault_search_rejects_negative_offset(vault_index: VaultIndex) -> None:
    """offset が負値のとき ValidationError."""
    from vault_search.validation import ValidationError

    fn = _fn(server_mod.vault_search)
    with pytest.raises((ValueError, ValidationError)):
        fn("obsidian", offset=-1)


def test_vault_search_rejects_limit_above_max(vault_index: VaultIndex) -> None:
    """limit > 500 は ValidationError。内部 _MAX_RESULTS 超えは silent truncate を避ける."""
    from vault_search.validation import ValidationError

    fn = _fn(server_mod.vault_search)
    with pytest.raises((ValueError, ValidationError)):
        fn("obsidian", limit=501)


def test_vault_recent_accepts_offset_parameter(vault_index: VaultIndex) -> None:
    """vault_recent に offset 引数があり、指定分スキップする."""
    fn = _fn(server_mod.vault_recent)
    full = fn(5, None)
    assert len(full["notes"]) >= 2, "テスト前提: 最低 2 件の recent notes が必要"
    skipped = fn(limit=5, offset=1)
    assert skipped["notes"] == full["notes"][1:], (
        "offset=1 は先頭 1 件を省いた結果を返すこと"
    )


def test_vault_recent_rejects_negative_offset(vault_index: VaultIndex) -> None:
    """vault_recent も offset 負値を拒否."""
    from vault_search.validation import ValidationError

    fn = _fn(server_mod.vault_recent)
    with pytest.raises((ValueError, ValidationError)):
        fn(limit=5, offset=-1)
