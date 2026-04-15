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
    res = fn(limit=5)
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
# metadata_filter (Issue #5) — MCP tool 経由の振る舞いを検証する。
# ---------------------------------------------------------------------------


def test_mcp_tool_vault_search_metadata_filter_eq(vault_index: VaultIndex) -> None:
    """eq 暗黙の metadata_filter で絞り込み."""
    fn = _fn(server_mod.vault_search)
    res = fn("obsidian", None, None, 20, 0, {"status": "active"})
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
        fn("obsidian", None, None, 20, 0, {"x": {"regex": "foo"}})


# ---------------------------------------------------------------------------
# FastMCP convert_result 経路でのレスポンス JSON 検証ヘルパ。
# ---------------------------------------------------------------------------


def _call_tool_structured(tool_name: str, arguments: dict) -> Any:
    """FastMCP の経路 (convert_result=True) でツールを実行し structured content を返す.

    全ツールは ``dict[str, Any]`` 戻り型に統一されているため、FastMCP の
    wrap_output は発動しない前提。万一 FastMCP upgrade 等で ``{"result": ...}``
    wrap が復活した場合は即座に気づけるよう明示的に assert する
    (サイレントな unwrap は regression 検知効果を弱めるため行わない)。
    """
    mgr = server_mod.mcp._tool_manager
    tool = mgr.get_tool(tool_name)
    assert tool is not None, f"tool not registered: {tool_name}"
    result = asyncio.run(tool.run(arguments, convert_result=True))
    # output_schema が定義されていれば (unstructured, structured) のタプル。
    assert isinstance(result, tuple), f"expected structured output tuple, got {type(result)}"
    _unstructured, structured = result
    if isinstance(structured, dict):
        assert set(structured.keys()) != {"result"}, (
            f"{tool_name}: structured content wrapped in 'result' (FastMCP wrap_output drift): "
            f"{structured!r}"
        )
    return structured


def test_vault_search_mcp_returns_all_fields(vault_index: VaultIndex) -> None:
    """MCP 経路で SearchHit の全フィールドが返る."""
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
    full = fn(limit=5)
    assert len(full["notes"]) >= 2, "テスト前提: 最低 2 件の recent notes が必要"
    skipped = fn(limit=5, offset=1)
    assert skipped["notes"] == full["notes"][1:], "offset=1 は先頭 1 件を省いた結果を返すこと"


def test_vault_recent_rejects_negative_offset(vault_index: VaultIndex) -> None:
    """vault_recent も offset 負値を拒否."""
    from vault_search.validation import ValidationError

    fn = _fn(server_mod.vault_recent)
    with pytest.raises((ValueError, ValidationError)):
        fn(limit=5, offset=-1)
