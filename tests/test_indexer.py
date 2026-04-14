"""indexer.py のテスト: VaultIndex + TieredCache."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from vault_search.indexer import TieredCache, VaultIndex
from vault_search.validation import ValidationError

# ---------------------------------------------------------------------------
# TieredCache
# ---------------------------------------------------------------------------


class TestTieredCache:
    def test_tier0_exact_hit(self) -> None:
        cache = TieredCache()
        result = [{"path": "a.md"}]
        cache.put("foo bar", None, result)
        tier, got = cache.get("foo bar", None)
        assert tier == 0
        assert got == result

    def test_tier1_fuzzy_hit(self) -> None:
        """Jaccard >= 0.8 で類似クエリがヒットする."""
        cache = TieredCache(fuzzy_threshold=0.8)
        # tokens: {"alpha", "beta", "gamma", "delta"}
        result = [{"path": "a.md"}]
        cache.put("alpha beta gamma delta", None, result)
        # tokens: {"alpha", "beta", "gamma", "delta", "epsilon"}
        # intersection=4, union=5 → 0.8 (境界)
        tier, got = cache.get("alpha beta gamma delta epsilon", None)
        assert tier == 1
        assert got == result

    def test_tier2_miss_low_similarity(self) -> None:
        cache = TieredCache(fuzzy_threshold=0.8)
        cache.put("alpha beta", None, [{"path": "a"}])
        tier, got = cache.get("totally different query here", None)
        assert tier == -1
        assert got is None

    def test_fuzzy_disabled_when_filters(self) -> None:
        """フィルタ付きは Tier 1 スキップ (完全一致のみ)."""
        cache = TieredCache()
        cache.put("alpha beta", {"tag": "x"}, [{"path": "a"}])
        # 別フィルタ・類似クエリ → ミス
        tier, got = cache.get("alpha beta gamma", {"tag": "y"})
        assert tier == -1
        assert got is None

    def test_invalidate_clears_all(self) -> None:
        cache = TieredCache()
        cache.put("q", None, [{"path": "a"}])
        cache.invalidate()
        tier, got = cache.get("q", None)
        assert tier == -1
        assert got is None

    def test_lru_eviction(self) -> None:
        cache = TieredCache(max_size=2)
        cache.put("a", None, [{"n": 1}])
        cache.put("b", None, [{"n": 2}])
        cache.put("c", None, [{"n": 3}])
        # "a" は押し出された
        tier, got = cache.get("a", None)
        assert tier == -1

    def test_ttl_expiry(self) -> None:
        cache = TieredCache(ttl=0.01)
        cache.put("q", None, [{"n": 1}])
        time.sleep(0.05)
        tier, got = cache.get("q", None)
        assert tier == -1


# ---------------------------------------------------------------------------
# VaultIndex — build / update / delete
# ---------------------------------------------------------------------------


def test_build_index_counts(vault_index: VaultIndex) -> None:
    stats = vault_index.stats()
    # `_archive/` と `.trash/` は除外。他 5 件が indexed
    assert stats["total_notes"] == 5


def test_excluded_prefix_folders(vault_index: VaultIndex) -> None:
    """`_` / `.` プレフィックスのフォルダは除外される."""
    folders = {f["folder"] for f in vault_index.list_folders()}
    # "_archive" / ".trash" が含まれないこと
    assert not any(f.startswith("_") for f in folders)
    assert not any(f.startswith(".") for f in folders)


def test_get_note_roundtrip(vault_index: VaultIndex) -> None:
    note = vault_index.get_note("Welcome.md")
    assert note is not None
    assert note["title"] == "Welcome"
    assert "intro" in note["tags"]
    assert note["aliases"] == ["Hello", "Intro"]


def test_get_note_missing(vault_index: VaultIndex) -> None:
    assert vault_index.get_note("does-not-exist.md") is None


def test_update_single_modifies(vault_index: VaultIndex, tmp_vault: Path) -> None:
    p = tmp_vault / "Welcome.md"
    p.write_text("---\ntitle: Updated\n---\nnew body\n", encoding="utf-8")
    ok = vault_index.update_single("Welcome.md")
    assert ok
    note = vault_index.get_note("Welcome.md")
    assert note is not None
    assert note["title"] == "Updated"
    assert "new body" in note["content"]


def test_update_single_deletes_when_missing(vault_index: VaultIndex, tmp_vault: Path) -> None:
    (tmp_vault / "Welcome.md").unlink()
    ok = vault_index.update_single("Welcome.md")
    assert ok
    assert vault_index.get_note("Welcome.md") is None


def test_update_single_rejects_traversal(vault_index: VaultIndex) -> None:
    # resolve() 後に vault_root を逸脱するパスは False
    ok = vault_index.update_single("../../etc/passwd")
    assert ok is False


def test_build_index_differential(vault_index: VaultIndex, tmp_vault: Path) -> None:
    """2 回目の build は差分のみ (skipped カウント)."""
    stats = vault_index.build_index()
    # 全てスキップされる
    assert stats["added"] == 0
    assert stats["updated"] == 0
    assert stats["skipped"] >= 1


def test_build_index_detects_new_file(vault_index: VaultIndex, tmp_vault: Path) -> None:
    (tmp_vault / "new.md").write_text("# New\n", encoding="utf-8")
    stats = vault_index.build_index()
    assert stats["added"] == 1


def test_build_index_detects_deletion(vault_index: VaultIndex, tmp_vault: Path) -> None:
    (tmp_vault / "Welcome.md").unlink()
    stats = vault_index.build_index()
    assert stats["deleted"] == 1


# ---------------------------------------------------------------------------
# VaultIndex — search
# ---------------------------------------------------------------------------


def test_search_basic_english(vault_index: VaultIndex) -> None:
    res = vault_index.search("obsidian")
    assert res["total"] >= 1
    paths = {r["path"] for r in res["results"]}
    assert "Welcome.md" in paths


def test_search_multibyte(vault_index: VaultIndex) -> None:
    res = vault_index.search("日本語")
    assert res["total"] >= 1
    paths = {r["path"] for r in res["results"]}
    assert "Projects/日本語ノート.md" in paths


def test_search_short_term_like_fallback(vault_index: VaultIndex) -> None:
    """3 文字未満のクエリは LIKE フォールバックで動く."""
    # trigram FTS5 は >= 3 文字必要。'ab' のような短い語でも落ちない
    res = vault_index.search("ab")
    assert isinstance(res["results"], list)


def test_search_tag_filter(vault_index: VaultIndex) -> None:
    res = vault_index.search("obsidian", tags=["intro"])
    paths = {r["path"] for r in res["results"]}
    # Welcome.md は intro を持つ、Research/alpha.md は持たない
    assert "Welcome.md" in paths
    assert "Research/alpha.md" not in paths


def test_search_folder_filter(vault_index: VaultIndex) -> None:
    res = vault_index.search("obsidian", folder="Research")
    for r in res["results"]:
        assert r["folder"].startswith("Research")


def test_search_tier0_cache_hit(vault_index: VaultIndex) -> None:
    """同一クエリ 2 回目は Tier 0."""
    vault_index.search("obsidian")
    res = vault_index.search("obsidian")
    assert res["tier"] == 0


def test_search_tier1_fuzzy(vault_index: VaultIndex) -> None:
    """類似クエリは Tier 1 fuzzy hit."""
    vault_index.search("alpha beta gamma delta")
    res = vault_index.search("alpha beta gamma delta epsilon")
    # Jaccard = 4/5 = 0.8 → ヒット
    assert res["tier"] == 1


def test_search_tier2_first_call(vault_index: VaultIndex) -> None:
    """新規クエリは Tier 2."""
    res = vault_index.search("unique-query-never-cached-xyzzy")
    assert res["tier"] == 2


def test_cache_invalidated_on_update(vault_index: VaultIndex, tmp_vault: Path) -> None:
    vault_index.search("obsidian")
    # キャッシュ存在 → update_single で無効化
    (tmp_vault / "Welcome.md").write_text("no match term\n", encoding="utf-8")
    vault_index.update_single("Welcome.md")
    res = vault_index.search("obsidian")
    # キャッシュ無効化されているので Tier 2
    assert res["tier"] == 2


def test_search_empty_query_returns_empty(vault_index: VaultIndex) -> None:
    res = vault_index.search("")
    # terms が空なら [] が返る
    assert res["results"] == []


def test_search_pagination(vault_index: VaultIndex) -> None:
    res1 = vault_index.search("obsidian", limit=1, offset=0)
    res2 = vault_index.search("obsidian", limit=1, offset=1)
    # 同じクエリなら total は一致
    assert res1["total"] == res2["total"]
    assert len(res1["results"]) <= 1


# ---------------------------------------------------------------------------
# VaultIndex — structured queries
# ---------------------------------------------------------------------------


def test_recent_notes(vault_index: VaultIndex) -> None:
    notes = vault_index.recent_notes(limit=10)
    assert len(notes) >= 1
    # 全てキーを持つ
    for n in notes:
        assert set(n.keys()) >= {
            "path",
            "title",
            "folder",
            "tags",
            "created_at",
            "modified_at",
        }


def test_recent_notes_folder_filter(vault_index: VaultIndex) -> None:
    notes = vault_index.recent_notes(limit=10, folder="Projects")
    for n in notes:
        assert n["folder"].startswith("Projects")


def test_list_tags(vault_index: VaultIndex) -> None:
    tags = vault_index.list_tags()
    tag_names = {t["tag"] for t in tags}
    assert "intro" in tag_names
    assert "project/alpha" in tag_names
    # _archive は除外されているので archived は無い
    assert "archived" not in tag_names


def test_list_folders(vault_index: VaultIndex) -> None:
    folders = vault_index.list_folders()
    names = {f["folder"] for f in folders}
    assert "Projects" in names
    assert "Research" in names


def test_list_folders_root_uses_empty_string(vault_index: VaultIndex) -> None:
    """vault_folders の結果の root 直下は '' であり '(root)' ではない.

    統一方針: SearchHit / RecentNote / NoteDetail と同じく FolderCount も
    root 直下は空文字。
    """
    rows = vault_index.list_folders()
    names = {r["folder"] for r in rows}
    # conftest の Welcome.md / malformed.md が root 直下にあるので "" が存在
    assert "" in names
    assert "(root)" not in names


def test_folder_prefix_does_not_match_sibling(
    vault_index: VaultIndex, tmp_vault: Path
) -> None:
    """folder='Projects' が 'Projects Hermes' のような兄弟を拾わないこと."""
    # 既存 fixture を壊さない範囲で兄弟フォルダを追加
    sibling = tmp_vault / "Projects Hermes" / "note.md"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_text(
        "---\ntitle: hermes\n---\nunique-hermes-marker content obsidian\n",
        encoding="utf-8",
    )
    vault_index.build_index()

    # search: folder='Projects' は 'Projects Hermes' 配下をマッチさせない
    res = vault_index.search("obsidian", folder="Projects")
    for r in res["results"]:
        assert r["folder"] == "Projects" or r["folder"].startswith("Projects/"), (
            f"sibling folder leaked in search: {r['folder']}"
        )
        assert not r["folder"].startswith("Projects Hermes")

    # recent_notes: 同じくシブリング除外
    notes = vault_index.recent_notes(limit=50, folder="Projects")
    for n in notes:
        assert n["folder"] == "Projects" or n["folder"].startswith("Projects/"), (
            f"sibling folder leaked in recent_notes: {n['folder']}"
        )
        assert not n["folder"].startswith("Projects Hermes")


def test_folder_filter_matches_folder_itself(
    vault_index: VaultIndex, tmp_vault: Path
) -> None:
    """folder='Projects' は 'Projects' 自体配下のノート (folder == 'Projects') を拾う."""
    # conftest の Projects/日本語ノート.md は folder=='Projects'
    res = vault_index.search("日本語", folder="Projects")
    paths = {r["path"] for r in res["results"]}
    assert "Projects/日本語ノート.md" in paths


def test_stats_shape(vault_index: VaultIndex) -> None:
    s = vault_index.stats()
    assert set(s.keys()) >= {
        "total_notes",
        "db_size_bytes",
        "db_size_mb",
        "vault_root",
    }
    assert s["total_notes"] > 0


# ---------------------------------------------------------------------------
# VaultIndex — metadata_filter (Issue #5)
# Red フェーズ: VaultIndex.search に metadata_filter 引数を追加する仕様。
# frontmatter 任意プロパティの AND フィルタ。eq (暗黙) / ne / in をサポート。
# ---------------------------------------------------------------------------


def test_metadata_filter_eq_implicit(vault_index: VaultIndex) -> None:
    """暗黙 eq: status=active で Welcome.md と Research/alpha.md のみ."""
    res = vault_index.search("obsidian", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    assert "Welcome.md" in paths
    assert "Research/alpha.md" in paths
    # 日本語ノートは status: draft なのでヒットしない
    assert "Projects/日本語ノート.md" not in paths


def test_metadata_filter_list_value_eq_contains(vault_index: VaultIndex) -> None:
    """リスト型 frontmatter の eq は「含む」判定 (tags と同様)."""
    res = vault_index.search("obsidian", metadata_filter={"categories": "work"})
    paths = {r["path"] for r in res["results"]}
    # Welcome.md: categories=[work, urgent] → work 含む
    assert "Welcome.md" in paths
    # Research/alpha.md: categories=[research] → work 含まない
    assert "Research/alpha.md" not in paths


def test_metadata_filter_in_operator(vault_index: VaultIndex) -> None:
    """in 演算: priority in [high, low] で Welcome.md と Research/alpha.md."""
    res = vault_index.search("obsidian", metadata_filter={"priority": {"in": ["high", "low"]}})
    paths = {r["path"] for r in res["results"]}
    assert "Welcome.md" in paths
    assert "Research/alpha.md" in paths
    # 日本語ノートは priority: medium → 除外
    assert "Projects/日本語ノート.md" not in paths


def test_metadata_filter_ne_operator(vault_index: VaultIndex) -> None:
    """ne 演算: status != draft で draft 以外がヒット."""
    res = vault_index.search("", metadata_filter={"status": {"ne": "draft"}})
    paths = {r["path"] for r in res["results"]}
    # Welcome.md / Research/alpha.md は status=active → ヒット
    assert "Welcome.md" in paths
    assert "Research/alpha.md" in paths
    # 日本語ノートは status: draft → 除外
    assert "Projects/日本語ノート.md" not in paths


def test_metadata_filter_ne_excludes_array_containing_value(
    vault_index: VaultIndex,
) -> None:
    """配列型 frontmatter に対する ne は「含まない」判定であること.

    Welcome.md は ``categories: [work, urgent]`` を持つので
    ``categories != work`` では除外されるべき (配列内に work を含むため)。
    Research/alpha.md は ``categories: [research]`` なので含まれるべき。
    """
    res = vault_index.search("", metadata_filter={"categories": {"ne": "work"}})
    paths = {r["path"] for r in res["results"]}
    # Welcome.md は categories に 'work' を含むので ne 'work' では除外
    assert "Welcome.md" not in paths, (
        "categories [work, urgent] は 'work' を含むので ne 'work' では除外されるべき"
    )
    # Research/alpha.md は categories: [research] → 'work' を含まない → ヒット
    assert "Research/alpha.md" in paths


def test_metadata_filter_multiple_keys_and(vault_index: VaultIndex) -> None:
    """複数キーは AND 結合: status=active AND priority=high → Welcome.md のみ."""
    res = vault_index.search(
        "",
        metadata_filter={"status": "active", "priority": "high"},
    )
    paths = {r["path"] for r in res["results"]}
    assert paths == {"Welcome.md"}


def test_metadata_filter_only_empty_query(vault_index: VaultIndex) -> None:
    """空クエリ + metadata_filter のみでも全件にフィルタ適用できる (新仕様)."""
    res = vault_index.search("", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    # 全件から status=active を抽出
    assert "Welcome.md" in paths
    assert "Research/alpha.md" in paths
    assert "Projects/日本語ノート.md" not in paths
    # plain.md は status キーなし → 除外
    assert "Projects/plain.md" not in paths


def test_metadata_filter_missing_key_excludes(vault_index: VaultIndex) -> None:
    """frontmatter にキー自体が無いノートは eq フィルタで除外される."""
    res = vault_index.search("", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    # plain.md は frontmatter 無し → status キーも無し → null と "active" は不一致
    assert "Projects/plain.md" not in paths


def test_metadata_filter_invalid_operator_raises(vault_index: VaultIndex) -> None:
    """未サポート演算子 (regex) は ValidationError / ValueError."""
    with pytest.raises((ValueError, ValidationError)):
        vault_index.search("obsidian", metadata_filter={"x": {"regex": "foo"}})


def test_metadata_filter_invalid_key_raises(vault_index: VaultIndex) -> None:
    """識別子ルール違反のキー名は ValidationError / ValueError."""
    with pytest.raises((ValueError, ValidationError)):
        vault_index.search("obsidian", metadata_filter={"../etc": "x"})


def test_metadata_filter_invalid_value_raises(vault_index: VaultIndex) -> None:
    """制御文字を含む値は ValidationError / ValueError."""
    with pytest.raises((ValueError, ValidationError)):
        vault_index.search("obsidian", metadata_filter={"status": "a\x00b"})
