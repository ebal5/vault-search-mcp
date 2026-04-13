"""server.py のテスト: 7 ツールの happy path + エラー."""

from __future__ import annotations

import pytest

from vault_search import server as server_mod
from vault_search.indexer import VaultIndex


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


def test_vault_get_note_missing_returns_error_shape() -> None:
    """server モジュールの挙動: 見つからないとき error dict."""
    idx = server_mod._get_index()
    # server 内の実装ロジックを模倣 (tool body と同じ)
    result = idx.get_note("does-not-exist.md")
    # 実際の tool 関数経由でもテスト
    if result is None:
        wrapped = {"error": "Note not found: does-not-exist.md"}
    else:
        wrapped = result
    assert "error" in wrapped


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
# ---------------------------------------------------------------------------


def test_mcp_tool_vault_search(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_search)
    res = fn("obsidian")
    assert "results" in res


def test_mcp_tool_vault_get_note_missing(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_get_note)
    res = fn("no/such/note.md")
    assert "error" in res


def test_mcp_tool_vault_get_note_found(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_get_note)
    res = fn("Welcome.md")
    assert res.get("title") == "Welcome"


def test_mcp_tool_vault_recent(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_recent)
    res = fn(5, None)
    assert isinstance(res, list)


def test_mcp_tool_vault_tags(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_tags)
    res = fn()
    assert isinstance(res, list)


def test_mcp_tool_vault_folders(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_folders)
    res = fn()
    assert isinstance(res, list)


def test_mcp_tool_vault_reindex(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_reindex)
    res = fn(False)
    assert "added" in res


def test_mcp_tool_vault_stats(vault_index: VaultIndex) -> None:
    fn = _fn(server_mod.vault_stats)
    res = fn()
    assert "total_notes" in res
