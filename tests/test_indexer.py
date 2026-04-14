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


def test_folder_prefix_does_not_match_sibling(vault_index: VaultIndex, tmp_vault: Path) -> None:
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


def test_folder_filter_matches_folder_itself(vault_index: VaultIndex, tmp_vault: Path) -> None:
    """folder='Projects' は 'Projects' 自体配下のノート (folder == 'Projects') を拾う."""
    # conftest の Projects/日本語ノート.md は folder=='Projects'
    res = vault_index.search("日本語", folder="Projects")
    paths = {r["path"] for r in res["results"]}
    assert "Projects/日本語ノート.md" in paths


def test_folder_filter_trailing_slash_normalized(
    vault_index: VaultIndex, tmp_vault: Path
) -> None:
    """末尾スラッシュ付き folder ('Projects/') も 'Projects' と同等に扱う.

    Issue #34: `folder='Projects/'` で silent 0 件になる regression の防止。
    """
    # search: 'Projects/' と 'Projects' は同じ結果を返す
    res_bare = vault_index.search("日本語", folder="Projects")
    res_slash = vault_index.search("日本語", folder="Projects/")
    paths_bare = {r["path"] for r in res_bare["results"]}
    paths_slash = {r["path"] for r in res_slash["results"]}
    assert paths_slash == paths_bare
    assert "Projects/日本語ノート.md" in paths_slash

    # recent_notes も同様
    notes_bare = vault_index.recent_notes(limit=50, folder="Projects")
    notes_slash = vault_index.recent_notes(limit=50, folder="Projects/")
    assert {n["path"] for n in notes_slash} == {n["path"] for n in notes_bare}


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
    """暗黙 eq: status=active で Welcome.md と Research/alpha.md のみ.

    total を厳密に検証することで、filter が無視されて全件返る regression を検知。
    """
    res = vault_index.search("obsidian", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    # Welcome.md + Research/alpha.md の 2 件
    assert res["total"] == 2, (
        f"expected exactly 2 hits for status=active, got total={res['total']} "
        f"(paths={sorted(paths)})"
    )
    assert paths == {"Welcome.md", "Research/alpha.md"}


def test_metadata_filter_list_value_eq_contains(vault_index: VaultIndex) -> None:
    """リスト型 frontmatter の eq は「含む」判定 (tags と同様).

    Control group: filter なし (baseline) より厳密に件数が減ることを確認。
    baseline の "obsidian" は Welcome + Research/alpha の 2 件ヒットするが、
    categories=work のフィルタでは Welcome の 1 件のみに絞られるべき。
    """
    baseline = vault_index.search("obsidian")
    baseline_paths = {r["path"] for r in baseline["results"]}
    assert baseline["total"] >= 2, (
        f"baseline must have >=2 hits for control group, "
        f"got {baseline['total']} (paths={sorted(baseline_paths)})"
    )

    res = vault_index.search("obsidian", metadata_filter={"categories": "work"})
    paths = {r["path"] for r in res["results"]}
    # Welcome.md のみ (categories=[work, urgent] → work 含む)
    assert res["total"] == 1, (
        f"expected exactly 1 hit for categories=work, got total={res['total']}"
    )
    assert paths == {"Welcome.md"}
    # Control group: filter 適用後は baseline より厳密に少ない
    assert res["total"] < baseline["total"], (
        f"metadata_filter must reduce hits vs baseline: "
        f"baseline={baseline['total']}, filtered={res['total']}"
    )


def test_metadata_filter_in_operator(vault_index: VaultIndex) -> None:
    """in 演算: priority in [high, low] で Welcome.md と Research/alpha.md."""
    res = vault_index.search("obsidian", metadata_filter={"priority": {"in": ["high", "low"]}})
    paths = {r["path"] for r in res["results"]}
    # Welcome.md (high) + Research/alpha.md (low) の 2 件
    assert res["total"] == 2, (
        f"expected exactly 2 hits for priority in [high, low], got total={res['total']}"
    )
    assert paths == {"Welcome.md", "Research/alpha.md"}


def test_metadata_filter_ne_operator(vault_index: VaultIndex) -> None:
    """ne 演算: status != draft で draft 以外がヒット.

    Control group: filter 無し (空クエリ + dummy filter で全件取得の代替) では
    "Projects/日本語ノート.md" (status=draft) も含まれるが、ne フィルタで
    除外されることを総件数で検証。
    """
    res = vault_index.search("", metadata_filter={"status": {"ne": "draft"}})
    paths = {r["path"] for r in res["results"]}
    # Welcome.md + Research/alpha.md は status=active → ヒット
    # 日本語ノート (draft) と plain.md / malformed.md (status キー無し) は除外
    assert res["total"] == 2, (
        f"expected exactly 2 hits for status ne draft, got total={res['total']} "
        f"(paths={sorted(paths)})"
    )
    assert paths == {"Welcome.md", "Research/alpha.md"}


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
    # categories キーを持つノートは Welcome / Research/alpha のみ。
    # Welcome は 'work' を含むので ne 'work' で除外 → Research/alpha 1 件
    assert res["total"] == 1, (
        f"expected exactly 1 hit for categories ne work, got total={res['total']} "
        f"(paths={sorted(paths)})"
    )
    assert paths == {"Research/alpha.md"}


def test_metadata_filter_multiple_keys_and(vault_index: VaultIndex) -> None:
    """複数キーは AND 結合: status=active AND priority=high → Welcome.md のみ.

    Control group: status=active 単独なら 2 件 (Welcome + Research/alpha)。
    priority=high を AND で追加することで Welcome の 1 件に絞られる。
    """
    single = vault_index.search("", metadata_filter={"status": "active"})
    assert single["total"] == 2, (
        f"control: status=active alone expected 2 hits, got {single['total']}"
    )

    res = vault_index.search(
        "",
        metadata_filter={"status": "active", "priority": "high"},
    )
    paths = {r["path"] for r in res["results"]}
    assert res["total"] == 1, (
        f"expected exactly 1 hit for status=active AND priority=high, "
        f"got total={res['total']} (paths={sorted(paths)})"
    )
    assert paths == {"Welcome.md"}
    # Control group: AND は単独条件より厳密に件数が減る
    assert res["total"] < single["total"]


def test_metadata_filter_only_empty_query(vault_index: VaultIndex) -> None:
    """空クエリ + metadata_filter のみでも全件にフィルタ適用できる (新仕様)."""
    res = vault_index.search("", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    # 全件から status=active を抽出 → Welcome.md + Research/alpha.md の 2 件
    assert res["total"] == 2, (
        f"expected exactly 2 hits for status=active (empty query), "
        f"got total={res['total']} (paths={sorted(paths)})"
    )
    assert paths == {"Welcome.md", "Research/alpha.md"}


def test_metadata_filter_missing_key_excludes(vault_index: VaultIndex) -> None:
    """frontmatter にキー自体が無いノートは eq フィルタで除外される."""
    res = vault_index.search("", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    # plain.md は frontmatter 無し → status キーも無し → "active" と不一致
    assert "Projects/plain.md" not in paths
    # total でも確認: status=active は 2 件のみ (frontmatter 欠損ノートは除外)
    assert res["total"] == 2


def test_metadata_filter_nonexistent_key_returns_empty(
    vault_index: VaultIndex,
) -> None:
    """存在しないキー (どのノートも持っていない) で eq すると total=0.

    Control group: 同じ空クエリで filter なし相当 (status=active) は 2 件
    返るのに対し、bogus_key での filter では 0 件に絞られることを確認。
    これにより「実装が filter 値を無視して全件返す」regression を検知する。
    """
    # Control: 存在するキーでの filter は >=1 件返る
    control = vault_index.search("", metadata_filter={"status": "active"})
    assert control["total"] >= 1

    res = vault_index.search("", metadata_filter={"bogus_key_xyzzy": "x"})
    assert res["total"] == 0, (
        f"expected 0 hits for nonexistent frontmatter key, got total={res['total']}"
    )
    assert res["results"] == []


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


# ---------------------------------------------------------------------------
# Issue #60: query の LIKE ワイルドカード / FTS5 特殊文字エスケープ
# ---------------------------------------------------------------------------


def test_search_like_underscore_not_wildcard(tmp_path: Path) -> None:
    """<3 文字 query の LIKE fallback で '_' がワイルドカード扱いされない.

    `query="_a"` (2 文字) は LIKE フォールバックに落ちる。従来は ``%_a%``
    として LIKE に差し込まれ、'_' が任意 1 文字扱いで 'Xa' 等も誤ヒットした。
    """
    root = tmp_path / "vault"
    root.mkdir()
    # 'Xa' を含むが '_a' は含まないノート (LIKE '_' ワイルドカード経由で誤ヒットする危険)
    (root / "bystander.md").write_text("# bystander\nthe Xa token here.\n", encoding="utf-8")
    # '_a' を含むノート (正例)
    (root / "target.md").write_text("# target\nliteral _a marker.\n", encoding="utf-8")
    idx = VaultIndex(root, db_path=tmp_path / "q.db")
    idx.build_index()

    res = idx.search("_a")
    paths = {r["path"] for r in res["results"]}
    assert "target.md" in paths
    assert "bystander.md" not in paths, (
        f"LIKE underscore leaked as wildcard: matched bystander.md (paths={paths})"
    )


def test_search_like_percent_not_wildcard(tmp_path: Path) -> None:
    """<3 文字 query の LIKE fallback で '%' がワイルドカード扱いされない."""
    root = tmp_path / "vault"
    root.mkdir()
    (root / "bystander.md").write_text("# bystander\nthe aZZZZb token.\n", encoding="utf-8")
    (root / "target.md").write_text("# target\nliteral %a marker.\n", encoding="utf-8")
    idx = VaultIndex(root, db_path=tmp_path / "q.db")
    idx.build_index()

    res = idx.search("%a")
    paths = {r["path"] for r in res["results"]}
    assert "target.md" in paths
    assert "bystander.md" not in paths, (
        f"LIKE percent leaked as wildcard: matched bystander.md (paths={paths})"
    )


def test_search_query_with_double_quote_does_not_raise(vault_index: VaultIndex) -> None:
    """query 内に `"` を含んでも sqlite3.OperationalError を漏らさない.

    従来は FTS5 path で ``f'"{t}"'`` に term をそのまま差し込むため、term 内の
    ``"`` が FTS5 構文エラーを引き起こし sqlite3.OperationalError が上流に漏れた。
    エスケープされていれば例外を上げず、通常の結果を返す (0 件でも可)。
    """
    import sqlite3

    try:
        res = vault_index.search('abc"def')
    except sqlite3.OperationalError as exc:  # pragma: no cover — regression
        raise AssertionError(f"FTS5 quote leaked as syntax error: {exc}") from exc
    # 結果の形式が壊れていないこと
    assert isinstance(res["results"], list)
