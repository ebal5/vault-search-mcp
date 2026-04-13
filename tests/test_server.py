"""server.py のテスト: 7 ツールの happy path + エラー."""

from __future__ import annotations

import pytest

from vault_search import server as server_mod
from vault_search.indexer import VaultIndex
from vault_search.schemas import (
    FolderCount,
    NoteDetail,
    NoteNotFoundError,
    RecentNote,
    ReindexStats,
    SearchResponse,
    TagCount,
    VaultStats,
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
    assert isinstance(res, SearchResponse)
    assert res.tier in (0, 1, 2)
    assert isinstance(res.total, int)
    assert isinstance(res.results, list)


def test_mcp_tool_vault_get_note_missing(vault_index: VaultIndex) -> None:
    """存在しないパスでは NoteNotFoundError を送出する (旧 error dict から変更)."""
    fn = _fn(server_mod.vault_get_note)
    with pytest.raises(NoteNotFoundError) as exc_info:
        fn("no/such/note.md")
    assert exc_info.value.path == "no/such/note.md"


def test_mcp_tool_vault_get_note_found(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_get_note)
    res = fn("Welcome.md")
    assert isinstance(res, NoteDetail)
    assert res.title == "Welcome"
    assert res.path == "Welcome.md"


def test_mcp_tool_vault_recent(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_recent)
    res = fn(5, None)
    assert isinstance(res, list)
    for item in res:
        assert isinstance(item, RecentNote)


def test_mcp_tool_vault_tags(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_tags)
    res = fn()
    assert isinstance(res, list)
    for item in res:
        assert isinstance(item, TagCount)
        assert isinstance(item.tag, str)
        assert isinstance(item.count, int)


def test_mcp_tool_vault_folders(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_folders)
    res = fn()
    assert isinstance(res, list)
    for item in res:
        assert isinstance(item, FolderCount)


def test_mcp_tool_vault_reindex(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_reindex)
    res = fn(False)
    assert isinstance(res, ReindexStats)
    # ReindexStats は added/updated/deleted/skipped/errors を必須フィールドで持つ
    assert res.added >= 0
    assert res.updated >= 0
    assert res.deleted >= 0
    assert res.skipped >= 0
    assert res.errors >= 0


def test_mcp_tool_vault_stats(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_stats)
    res = fn()
    assert isinstance(res, VaultStats)
    assert res.total_notes > 0
    assert res.db_size_bytes >= 0
    assert res.vault_root  # non-empty path string
