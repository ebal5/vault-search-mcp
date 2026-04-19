"""indexer.py のテスト: VaultIndex + TieredCache."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from vault_search.cache import TieredCache
from vault_search.indexer import VaultIndex
from vault_search.validation import ValidationError

# ---------------------------------------------------------------------------
# TieredCache
# ---------------------------------------------------------------------------


class TestTieredCache:
    def test_tier0_exact_hit(self) -> None:
        cache = TieredCache()
        result = [{"path": "a.md"}]
        cache.put("foo bar", None, result, total=1)
        tier, entry = cache.get("foo bar", None)
        assert tier == 0
        assert entry is not None
        assert entry.result == result
        assert entry.total == 1

    def test_tier1_fuzzy_hit(self) -> None:
        """threshold を明確に上回る Jaccard (9/10=0.9) でヒットする."""
        cache = TieredCache(fuzzy_threshold=0.8)
        result = [{"path": "a.md"}]
        # tokens: {"a","b","c","d","e","f","g","h","i"} — 9 tokens
        cache.put("a b c d e f g h i", None, result, total=1)
        # tokens: {"a","b","c","d","e","f","g","h","i","j"} — 10 tokens
        # intersection=9, union=10 → Jaccard=9/10=0.9 > 0.8
        tier, entry = cache.get("a b c d e f g h i j", None)
        assert tier == 1
        assert entry is not None
        assert entry.result == result

    def test_tier1_fuzzy_hit_at_threshold(self) -> None:
        """Jaccard が threshold 丁度 (4/5=0.8) でもヒットする — >= semantics を pin."""
        cache = TieredCache(fuzzy_threshold=0.8)
        result = [{"path": "a.md"}]
        cache.put("alpha beta gamma delta", None, result, total=1)
        # intersection=4, union=5 → Jaccard=4/5=0.8 (= threshold)
        tier, entry = cache.get("alpha beta gamma delta epsilon", None)
        assert tier == 1
        assert entry is not None
        assert entry.result == result

    def test_tier1_fuzzy_miss_just_below_threshold(self) -> None:
        """Jaccard が threshold 未満 (7/9≈0.778) でミスになる."""
        cache = TieredCache(fuzzy_threshold=0.8)
        result = [{"path": "a.md"}]
        # tokens: {"a","b","c","d","e","f","g"} — 7 tokens
        cache.put("a b c d e f g", None, result, total=1)
        # tokens: {"a","b","c","d","e","f","g","h","i"} — 9 tokens
        # intersection=7, union=9 → Jaccard=7/9≈0.778 < 0.8
        tier, entry = cache.get("a b c d e f g h i", None)
        assert tier == -1
        assert entry is None

    def test_tier2_miss_low_similarity(self) -> None:
        cache = TieredCache(fuzzy_threshold=0.8)
        cache.put("alpha beta", None, [{"path": "a"}], total=1)
        tier, entry = cache.get("totally different query here", None)
        assert tier == -1
        assert entry is None

    def test_fuzzy_disabled_when_filters(self) -> None:
        """フィルタ付きは Tier 1 スキップ (完全一致のみ)."""
        cache = TieredCache()
        cache.put("alpha beta", {"tag": "x"}, [{"path": "a"}], total=1)
        # 別フィルタ・類似クエリ → ミス
        tier, entry = cache.get("alpha beta gamma", {"tag": "y"})
        assert tier == -1
        assert entry is None

    def test_invalidate_clears_all(self) -> None:
        cache = TieredCache()
        cache.put("q", None, [{"path": "a"}], total=1)
        cache.invalidate()
        tier, entry = cache.get("q", None)
        assert tier == -1
        assert entry is None

    def test_lru_eviction(self) -> None:
        cache = TieredCache(max_size=2)
        cache.put("a", None, [{"n": 1}], total=1)
        cache.put("b", None, [{"n": 2}], total=1)
        cache.put("c", None, [{"n": 3}], total=1)
        # "a" は押し出された
        tier, entry = cache.get("a", None)
        assert tier == -1
        assert entry is None

    def test_ttl_expiry(self) -> None:
        cache = TieredCache(ttl=0.01)
        cache.put("q", None, [{"n": 1}], total=1)
        time.sleep(0.05)
        tier, entry = cache.get("q", None)
        assert tier == -1
        assert entry is None

    def test_entry_preserves_total_above_result_len(self) -> None:
        """Issue #17: result が cap で truncate されたとき entry.total は
        accurate な件数を保持する."""
        cache = TieredCache()
        cache.put("q", None, [{"n": 1}, {"n": 2}], total=1234)
        tier, entry = cache.get("q", None)
        assert tier == 0
        assert entry is not None
        assert len(entry.result) == 2
        assert entry.total == 1234


# ---------------------------------------------------------------------------
# VaultIndex — build / update / delete
# ---------------------------------------------------------------------------


def test_build_index_counts(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """indexable note は正確にカウントされ、`_` / `.` 始まりフォルダは除外される."""
    _root, idx = vault_builder(
        {
            "a.md": "# a\n",
            "b.md": "# b\n",
            "sub/c.md": "# c\n",
            "_archive/old.md": "# excluded (underscore prefix)\n",
            ".trash/del.md": "# excluded (dot prefix)\n",
        }
    )
    stats = idx.stats()
    assert stats["total_notes"] == 3


def test_excluded_prefix_folders(vault_index: VaultIndex) -> None:
    """`_` / `.` プレフィックスのフォルダは除外される."""
    folders = {f["folder"] for f in vault_index.list_folders()}
    # "_archive" / ".trash" が含まれないこと
    assert not any(f.startswith("_") for f in folders)
    assert not any(f.startswith(".") for f in folders)


# ---------------------------------------------------------------------------
# is_indexable_path — walker と watcher Handler の単一ソース (#76)
# ---------------------------------------------------------------------------


class TestIsIndexablePath:
    """``VaultIndex.is_indexable_path`` の除外ルール (#76)."""

    def test_accepts_flat_md(self, tmp_path: Path) -> None:
        idx = VaultIndex(tmp_path, db_path=tmp_path / "x.db")
        assert idx.is_indexable_path(tmp_path / "note.md") == "note.md"

    def test_accepts_nested_md(self, tmp_path: Path) -> None:
        idx = VaultIndex(tmp_path, db_path=tmp_path / "x.db")
        assert idx.is_indexable_path(tmp_path / "sub" / "deep" / "n.md") == "sub/deep/n.md"

    def test_rejects_non_md_extension(self, tmp_path: Path) -> None:
        idx = VaultIndex(tmp_path, db_path=tmp_path / "x.db")
        assert idx.is_indexable_path(tmp_path / "notes.txt") is None
        assert idx.is_indexable_path(tmp_path / "image.png") is None
        assert idx.is_indexable_path(tmp_path / "README.MD") is None  # case-sensitive

    def test_rejects_outside_vault(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        idx = VaultIndex(vault, db_path=tmp_path / "x.db")
        outside = tmp_path / "elsewhere" / "x.md"
        assert idx.is_indexable_path(outside) is None

    @pytest.mark.parametrize(
        "rel,expect",
        [
            (".archive/n.md", None),
            ("_drafts/n.md", None),
            ("sub/.hidden/n.md", None),
            ("sub/_internal/n.md", None),
            (".archive.md", None),  # ファイル名自体も除外
            ("_private.md", None),
        ],
    )
    def test_rejects_excluded_prefix_segments(
        self, tmp_path: Path, rel: str, expect: str | None
    ) -> None:
        idx = VaultIndex(tmp_path, db_path=tmp_path / "x.db")
        assert idx.is_indexable_path(tmp_path / rel) is expect

    def test_accepts_string_and_path_inputs(self, tmp_path: Path) -> None:
        idx = VaultIndex(tmp_path, db_path=tmp_path / "x.db")
        assert idx.is_indexable_path(str(tmp_path / "n.md")) == "n.md"
        assert idx.is_indexable_path(tmp_path / "n.md") == "n.md"


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
    """threshold を上回る Jaccard (5/6≈0.833) で Tier 1 fuzzy hit."""
    vault_index.search("alpha beta gamma delta epsilon")
    res = vault_index.search("alpha beta gamma delta epsilon zeta")
    # intersection=5, union=6 → Jaccard=5/6≈0.833 > 0.8 → ヒット
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


# ---------------------------------------------------------------------------
# Issue #17: total が _MAX_RESULTS=500 で truncate される問題。
# 巨大 vault で agent がページング終端を誤認しないよう accurate な total と
# truncated フラグを返すことを検証する。
# cap 値変更への耐性のため VaultIndex._MAX_RESULTS を参照する (ハードコード回避)。
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_vault_over_cap(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> tuple[Path, VaultIndex, int]:
    """cap+1 件の tag + FTS 可能トークンを持つ vault を 1 度だけ構築する.

    本文に 5 語の共通トークンを入れてあるのは、tier=1 fuzzy cache hit
    (Jaccard >= 0.8) を成立させる類似クエリを組めるようにするため。
    """
    cap = VaultIndex._MAX_RESULTS
    # "obsidian-bulk" は FTS5 trigram (3 文字以上) を通るマーカー。
    # alpha/beta/gamma/delta は tier=1 テストで Jaccard 閾値を跨ぐために追加。
    body = "alpha beta gamma delta obsidian-bulk\n"
    notes = {f"bulk/note_{i:04d}.md": f"---\ntags: [bulk-tag]\n---\n{body}" for i in range(cap + 1)}
    root, idx = vault_builder(notes)
    return root, idx, cap


def test_search_total_accurate_beyond_max_results_filter_only(
    bulk_vault_over_cap: tuple[Path, VaultIndex, int],
) -> None:
    """Issue #17: filter-only パスで total が cap で truncate されない."""
    _root, idx, cap = bulk_vault_over_cap
    res = idx.search("", tags=["bulk-tag"], limit=50, offset=0)
    assert res["total"] == cap + 1, (
        f"total は accurate な件数 ({cap + 1}) を返すこと; got {res['total']}"
    )
    assert res["truncated"] is True
    assert len(res["results"]) == 50


def test_search_total_accurate_beyond_max_results_fts_path(
    bulk_vault_over_cap: tuple[Path, VaultIndex, int],
) -> None:
    """Issue #17: FTS5 パスでも COUNT(*) が正しく発行され total が accurate.

    filter-only パスとは別の SQL (notes_fts JOIN notes + MATCH) を通るため、
    独立に regression guard する。
    """
    _root, idx, cap = bulk_vault_over_cap
    # "obsidian-bulk" は 13 文字 (>=3) で hyphen 単一語なので fts_terms に入る。
    # 全 501 件がこのトークンを本文に含むため、FTS5 MATCH で cap+1 件ヒット。
    # LIKE フォールバック経路 (3 文字未満) とは別の SQL を通る点が本 test の要。
    res = idx.search("obsidian-bulk", limit=10, offset=0)
    assert res["total"] == cap + 1, (
        f"FTS5 パスでも total が accurate に ({cap + 1}) 返ること; got {res['total']}"
    )
    assert res["truncated"] is True
    # FTS5 が実際にマッチしたことを results 数で担保 (空なら LIKE fallback か FTS 失敗)
    assert len(res["results"]) == 10


def test_search_truncated_flag_false_exactly_at_cap(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """Issue #17: total == _MAX_RESULTS (ちょうど cap) のとき truncated=False.

    境界判定が `>=` に劣化すると false positive の truncated になる regression。
    """
    cap = VaultIndex._MAX_RESULTS
    notes = {f"edge/note_{i:04d}.md": "---\ntags: [edge]\n---\nbody\n" for i in range(cap)}
    _root, idx = vault_builder(notes)
    res = idx.search("", tags=["edge"])
    assert res["total"] == cap
    assert res["truncated"] is False, (
        f"total=={cap} は cap 以内なので truncated=False; got {res['truncated']}"
    )


def test_search_truncated_flag_false_below_cap(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """Issue #17: total <= _MAX_RESULTS のとき truncated=False."""
    notes = {f"a_{i}.md": "---\ntags: [few]\n---\nbody\n" for i in range(3)}
    _root, idx = vault_builder(notes)
    res = idx.search("", tags=["few"])
    assert res["truncated"] is False


def test_search_cache_hit_preserves_truncated_and_total(
    bulk_vault_over_cap: tuple[Path, VaultIndex, int],
) -> None:
    """Issue #17: Tier 0 cache hit 時も total と truncated が正しく返る.

    同一クエリを 2 回実行し、2 回目が Tier 0 で total/truncated を entry.total から
    正しく再構成していることを確認する (cache API の total 伝達 regression guard)。
    """
    _root, idx, cap = bulk_vault_over_cap
    first = idx.search("", tags=["bulk-tag"], limit=5)
    assert first["tier"] == 2
    second = idx.search("", tags=["bulk-tag"], limit=5)
    assert second["tier"] == 0, "同一クエリは Tier 0 に乗る"
    assert second["total"] == cap + 1, "cache hit でも total は accurate"
    assert second["truncated"] is True, "cache hit でも truncated が伝達される"


def test_search_tier1_fuzzy_preserves_truncated(
    bulk_vault_over_cap: tuple[Path, VaultIndex, int],
) -> None:
    """Issue #17: Tier 1 fuzzy cache hit でも truncated フラグが保持される.

    tier=1 は filters 無しで Jaccard >= 0.8 の類似クエリで発動する。
    キャッシュされた entry.total (cap+1) と truncated 判定が再利用されることを
    確認する (cache.get が entry を返す契約 regression guard)。
    """
    _root, idx, cap = bulk_vault_over_cap
    # prime cache: 5 tokens (filter 無し → tier 1 候補になる)
    first = idx.search("alpha beta gamma delta obsidian-bulk", limit=5)
    assert first["tier"] == 2
    assert first["truncated"] is True
    # Jaccard = 5 / 6 ≈ 0.833 > 0.8 → tier=1 hit。
    # "epsilon" は本文に無いため FTS 再実行なら 0 件だが、cache reuse で prime の結果が返る。
    second = idx.search("alpha beta gamma delta obsidian-bulk epsilon", limit=5)
    assert second["tier"] == 1, f"fuzzy hit しなかった: tier={second['tier']}"
    assert second["total"] == cap + 1, "tier=1 は prime クエリの total を再利用"
    assert second["truncated"] is True, "tier=1 でも truncated が保持される"


def test_folder_prefix_does_not_match_sibling(vault_index: VaultIndex, tmp_vault: Path) -> None:
    """folder='Projects' が 'Projects Hermes' のような兄弟を拾わないこと."""
    # 兄弟フォルダに同一トークンを含むノートを置く。
    # folder フィルタが壊れていればこのノートが漏れ込み、アサートが失敗する。
    sibling = tmp_vault / "Projects Hermes" / "note.md"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_text(
        "---\ntitle: hermes\n---\nobsidian-marker-projects content in sibling\n",
        encoding="utf-8",
    )
    vault_index.build_index()

    # search: folder='Projects' は 'Projects Hermes' 配下をマッチさせない。
    # conftest の Projects/marker.md が "obsidian-marker-projects" を含むため
    # 正しい実装では必ず >= 1 件ヒットする (vacuous pass 防止)。
    res = vault_index.search("obsidian-marker-projects", folder="Projects")
    assert len(res["results"]) >= 1, (
        "Projects/marker.md がヒットしない — fixture または検索の不具合"
    )
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


def test_folder_filter_trailing_slash_normalized(vault_index: VaultIndex, tmp_vault: Path) -> None:
    """末尾スラッシュ付き folder ('Projects/') も 'Projects' と同等に扱う.

    Issue #34: `folder='Projects/'` で silent 0 件になる regression の防止。

    境界値:
    - 単一 `/` 付与 (`Projects/`)
    - 複数 `/` 付与 (`Projects//`)
    - バックスラッシュ混在 (`Projects\\`)
    - バックスラッシュ + `/` (`Projects\\/`)
    すべて `Projects` と同一結果となる contract を pin する。
    """
    # 基準 (非空) — vacuous pass 防止のため最初に非空を assert
    res_bare = vault_index.search("日本語", folder="Projects")
    paths_bare = {r["path"] for r in res_bare["results"]}
    assert "Projects/日本語ノート.md" in paths_bare, (
        "fixture regression: bare folder search returned empty"
    )

    notes_bare = vault_index.recent_notes(limit=50, folder="Projects")
    bare_note_paths = {n["path"] for n in notes_bare}
    assert "Projects/日本語ノート.md" in bare_note_paths, (
        "fixture regression: bare folder recent_notes returned empty"
    )

    # 正規化バリアント: いずれも Projects と同一結果
    for variant in ("Projects/", "Projects//", "Projects\\", "Projects\\/"):
        res_v = vault_index.search("日本語", folder=variant)
        paths_v = {r["path"] for r in res_v["results"]}
        assert paths_v == paths_bare, f"search mismatch for folder={variant!r}: {paths_v}"

        notes_v = vault_index.recent_notes(limit=50, folder=variant)
        assert {n["path"] for n in notes_v} == bare_note_paths, (
            f"recent_notes mismatch for folder={variant!r}"
        )


def test_folder_filter_slash_only_is_noop(vault_index: VaultIndex, tmp_vault: Path) -> None:
    """`folder='/'` は rstrip 後に空文字化するので **フィルタなし** 扱いとなる.

    Issue #34 推奨: 「空文字列ケース (`/` だけが渡ったケース) も rstrip 後に
    空なら no-op で扱う」。rstrip 後 '' で `folder = '' OR folder LIKE '/%'`
    を発行し root 直下のみ silent にマッチする旧挙動への regression 防止。
    """
    # 基準 (folder 未指定 = フィルタなし) との一致を確認
    res_all = vault_index.search("obsidian")
    paths_all = {r["path"] for r in res_all["results"]}
    assert len(paths_all) > 0, "fixture regression: no results for 'obsidian'"

    res_slash = vault_index.search("obsidian", folder="/")
    assert {r["path"] for r in res_slash["results"]} == paths_all

    # 複数スラッシュも同等 (`//`, `\\`, `\\/` 等)
    for variant in ("//", "\\", "\\/", "/\\"):
        res_v = vault_index.search("obsidian", folder=variant)
        assert {r["path"] for r in res_v["results"]} == paths_all, (
            f"folder={variant!r} expected to be no-op (== no filter), got differing set"
        )


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
# frontmatter 任意プロパティの AND フィルタ。eq (暗黙) / ne / in をサポート。
#
# Issue #26: SAMPLE_NOTES (Welcome.md / Research/alpha.md 等の共有 fixture) に
# 依存せず、各テストが ``vault_builder`` で最小 vault を組む。これによって
# SAMPLE_NOTES に無関係なキー (例えば将来の別テストで追加される tag) が
# 紛れ込んでも filter の total が変わらない独立性を得る。
# ---------------------------------------------------------------------------

# metadata_filter の AND / eq / ne / in 検証用の共通ノート集合。
# 全 filter テストで使い回せるよう、status / priority / categories の 3 キーに
# 多様性 (active/draft/no-key; high/low/medium; work/urgent/research) を持たせる。
_META_FILTER_NOTES: dict[str, str] = {
    "alpha.md": (
        "---\n"
        "status: active\n"
        "priority: high\n"
        "categories:\n"
        "  - work\n"
        "  - urgent\n"
        "---\n"
        "alpha body with obsidian token.\n"
    ),
    "beta.md": (
        "---\n"
        "status: active\n"
        "priority: low\n"
        "categories:\n"
        "  - research\n"
        "---\n"
        "beta body with obsidian token.\n"
    ),
    "gamma.md": ("---\nstatus: draft\npriority: medium\n---\ngamma body (no categories).\n"),
    "plain.md": "# Plain\n\nno frontmatter at all.\n",
}


def test_metadata_filter_eq_implicit(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """暗黙 eq: status=active で該当 2 件のみ.

    total を厳密に検証することで、filter が無視されて全件返る regression を検知。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("obsidian", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    # alpha.md + beta.md の 2 件 (いずれも status=active かつ "obsidian" を含む)
    assert res["total"] == 2, (
        f"expected exactly 2 hits for status=active, got total={res['total']} "
        f"(paths={sorted(paths)})"
    )
    assert paths == {"alpha.md", "beta.md"}


def test_metadata_filter_list_value_eq_contains(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """リスト型 frontmatter の eq は「含む」判定 (tags と同様).

    Control group: filter なし (baseline) より厳密に件数が減ることを確認。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    baseline = idx.search("obsidian")
    baseline_paths = {r["path"] for r in baseline["results"]}
    assert baseline["total"] >= 2, (
        f"baseline must have >=2 hits for control group, "
        f"got {baseline['total']} (paths={sorted(baseline_paths)})"
    )

    res = idx.search("obsidian", metadata_filter={"categories": "work"})
    paths = {r["path"] for r in res["results"]}
    # alpha.md のみ (categories=[work, urgent] → work 含む)
    assert res["total"] == 1, (
        f"expected exactly 1 hit for categories=work, got total={res['total']}"
    )
    assert paths == {"alpha.md"}
    # Control group: filter 適用後は baseline より厳密に少ない
    assert res["total"] < baseline["total"], (
        f"metadata_filter must reduce hits vs baseline: "
        f"baseline={baseline['total']}, filtered={res['total']}"
    )


def test_metadata_filter_in_operator(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """in 演算: priority in [high, low] で該当 2 件."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("obsidian", metadata_filter={"priority": {"in": ["high", "low"]}})
    paths = {r["path"] for r in res["results"]}
    # alpha.md (high) + beta.md (low) の 2 件
    assert res["total"] == 2, (
        f"expected exactly 2 hits for priority in [high, low], got total={res['total']}"
    )
    assert paths == {"alpha.md", "beta.md"}


def test_metadata_filter_ne_operator(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """ne 演算: status != draft で draft 以外がヒット.

    Control group: filter 無しでは gamma.md (status=draft) も含まれるが、
    ne フィルタで除外されることを総件数で検証。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("", metadata_filter={"status": {"ne": "draft"}})
    paths = {r["path"] for r in res["results"]}
    # alpha.md + beta.md は status=active → ヒット
    # gamma.md (draft) と plain.md (status キー無し) は除外
    assert res["total"] == 2, (
        f"expected exactly 2 hits for status ne draft, got total={res['total']} "
        f"(paths={sorted(paths)})"
    )
    assert paths == {"alpha.md", "beta.md"}


def test_metadata_filter_ne_excludes_array_containing_value(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """配列型 frontmatter に対する ne は「含まない」判定であること.

    alpha.md は ``categories: [work, urgent]`` を持つので
    ``categories != work`` では除外されるべき (配列内に work を含むため)。
    beta.md は ``categories: [research]`` なので含まれるべき。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("", metadata_filter={"categories": {"ne": "work"}})
    paths = {r["path"] for r in res["results"]}
    # categories キーを持つノートは alpha / beta のみ。
    # alpha は 'work' を含むので ne 'work' で除外 → beta 1 件
    assert res["total"] == 1, (
        f"expected exactly 1 hit for categories ne work, got total={res['total']} "
        f"(paths={sorted(paths)})"
    )
    assert paths == {"beta.md"}


def test_metadata_filter_multiple_keys_and(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """複数キーは AND 結合: status=active AND priority=high → alpha.md のみ.

    Control group: status=active 単独なら 2 件 (alpha + beta)。
    priority=high を AND で追加することで alpha の 1 件に絞られる。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    single = idx.search("", metadata_filter={"status": "active"})
    assert single["total"] == 2, (
        f"control: status=active alone expected 2 hits, got {single['total']}"
    )

    res = idx.search(
        "",
        metadata_filter={"status": "active", "priority": "high"},
    )
    paths = {r["path"] for r in res["results"]}
    assert res["total"] == 1, (
        f"expected exactly 1 hit for status=active AND priority=high, "
        f"got total={res['total']} (paths={sorted(paths)})"
    )
    assert paths == {"alpha.md"}
    # Control group: AND は単独条件より厳密に件数が減る
    assert res["total"] < single["total"]


def test_metadata_filter_only_empty_query(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """空クエリ + metadata_filter のみでも全件にフィルタ適用できる (新仕様)."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    # 全件から status=active を抽出 → alpha.md + beta.md の 2 件
    assert res["total"] == 2, (
        f"expected exactly 2 hits for status=active (empty query), "
        f"got total={res['total']} (paths={sorted(paths)})"
    )
    assert paths == {"alpha.md", "beta.md"}


def test_metadata_filter_missing_key_excludes(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """frontmatter にキー自体が無いノートは eq フィルタで除外される."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("", metadata_filter={"status": "active"})
    paths = {r["path"] for r in res["results"]}
    # plain.md は frontmatter 無し → status キーも無し → "active" と不一致
    assert "plain.md" not in paths
    # total でも確認: status=active は 2 件のみ (frontmatter 欠損ノートは除外)
    assert res["total"] == 2


def test_metadata_filter_nonexistent_key_raises(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """存在しないキーは ValidationError (Issue #119).

    indexer が自身で ``list_frontmatter_keys()`` を呼んで known_keys を自己解決
    する設計変更に伴い、未知キーは silent に 0 件を返すのではなく
    ``UNKNOWN_FRONTMATTER_KEY`` で即座に拒否する。

    Control: 存在するキーでの filter は引き続き >=1 件返る
    (filter 値が無視されていない regression guard)。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    control = idx.search("", metadata_filter={"status": "active"})
    assert control["total"] >= 1

    with pytest.raises(ValidationError) as exc:
        idx.search("", metadata_filter={"bogus_key_xyzzy": "x"})
    assert exc.value.error_code == "UNKNOWN_FRONTMATTER_KEY"


def test_search_rejects_known_keys_kwarg(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """Issue #119: known_keys パラメータは削除された (leaky abstraction 解消)."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    with pytest.raises(TypeError):
        idx.search(  # type: ignore[call-arg]
            "",
            metadata_filter={"status": "active"},
            known_keys=["status"],
        )


def test_metadata_filter_invalid_operator_raises(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """未サポート演算子 (regex) は ValidationError / ValueError."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    with pytest.raises((ValueError, ValidationError)):
        idx.search("obsidian", metadata_filter={"x": {"regex": "foo"}})


def test_metadata_filter_invalid_key_raises(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """識別子ルール違反のキー名は ValidationError / ValueError."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    with pytest.raises((ValueError, ValidationError)):
        idx.search("obsidian", metadata_filter={"../etc": "x"})


def test_metadata_filter_invalid_value_raises(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """制御文字を含む値は ValidationError / ValueError."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    with pytest.raises((ValueError, ValidationError)):
        idx.search("obsidian", metadata_filter={"status": "a\x00b"})


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


# ---------------------------------------------------------------------------
# End-to-end regression guards for Issue #15 / #49:
# YAML frontmatter の非 str 型 (int / bool / float / date) が metadata_filter
# の str 値で正しくマッチする (parse 時に文字列正規化される前提).
# ---------------------------------------------------------------------------


def test_metadata_filter_matches_int_frontmatter(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """YAML int ``priority: 5`` が metadata_filter `{"priority": "5"}` でヒット."""
    _root, idx = vault_builder(
        {
            "hi.md": "---\npriority: 5\n---\nbody\n",
            "lo.md": "---\npriority: 3\n---\nbody\n",
        },
    )
    res = idx.search("", metadata_filter={"priority": "5"})
    paths = {r["path"] for r in res["results"]}
    assert paths == {"hi.md"}, f"int priority=5 did not match: got {paths}"


def test_metadata_filter_matches_bool_frontmatter(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """YAML bool ``archived: true`` が metadata_filter `{"archived": "true"}` でヒット.

    ``"1"``/``"0"`` UX ワートが解消されていることを確認するガード。
    """
    _root, idx = vault_builder(
        {
            "a.md": "---\narchived: true\n---\nbody\n",
            "b.md": "---\narchived: false\n---\nbody\n",
        },
    )
    res = idx.search("", metadata_filter={"archived": "true"})
    paths = {r["path"] for r in res["results"]}
    assert paths == {"a.md"}, f"bool archived=true did not match: got {paths}"


def test_metadata_filter_ne_bool_frontmatter(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """bool の ne も "true"/"false" 文字列で正しく除外される (#49 対称)."""
    _root, idx = vault_builder(
        {
            "a.md": "---\narchived: true\n---\nbody\n",
            "b.md": "---\narchived: false\n---\nbody\n",
        },
    )
    res = idx.search("", metadata_filter={"archived": {"ne": "true"}})
    paths = {r["path"] for r in res["results"]}
    assert paths == {"b.md"}, f"bool ne 'true' mismatch: got {paths}"


def test_metadata_filter_mixed_scalar_types_end_to_end(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """全スカラー型 (int/bool/float/date) の正規化 → filter 経路を 1 テストで検証.

    個別型テスト (``matches_int_frontmatter`` / ``matches_bool_frontmatter``) は
    単一型のみをカバーしているため、float / date の正規化が部分的に壊れた場合に
    silent regression となりうる (Round 3 Reviewer C finding 1)。本テストは
    parse → index → filter の full pipeline を全スカラー型で一括検証する。
    """
    _root, idx = vault_builder(
        {
            "target.md": (
                "---\npriority: 5\narchived: true\nscore: 3.7\ndue: 2024-01-15\n---\nbody\n"
            ),
            "other.md": (
                "---\npriority: 3\narchived: false\nscore: 1.2\ndue: 2024-06-01\n---\nbody\n"
            ),
        },
    )
    for key, val in [
        ("priority", "5"),
        ("archived", "true"),
        ("score", "3.7"),
        ("due", "2024-01-15"),
    ]:
        res = idx.search("", metadata_filter={key: val})
        paths = {r["path"] for r in res["results"]}
        assert paths == {"target.md"}, f"{key}={val!r} did not match exactly target.md: got {paths}"


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


# ---------------------------------------------------------------------------
# Issue #80: metadata_filter_diagnostics when total == 0
# ---------------------------------------------------------------------------


def test_metadata_filter_diagnostics_attached_on_zero_total(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """0 件 + metadata_filter 指定時は diagnostics が付与される (Issue #80).

    エージェントが「キーは存在するが値が全件不一致」なのか区別できるよう、
    filter に使った各キーの observed_values_sample を添える。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    # status キーは存在するが "nonexistent_value" は index 内のどの値とも不一致
    res = idx.search("", metadata_filter={"status": "nonexistent_value"})
    assert res["total"] == 0
    assert "metadata_filter_diagnostics" in res, (
        "0 件 + metadata_filter 指定時は metadata_filter_diagnostics が必要 (Issue #80)"
    )
    diag = res["metadata_filter_diagnostics"]
    assert isinstance(diag, list) and len(diag) == 1
    entry = diag[0]
    assert entry["key"] == "status"
    assert entry["key_present_in_index"] is True
    # _META_FILTER_NOTES は status = active (alpha,beta) / draft (gamma)。
    # sample_values は頻度降順なので active → draft の順で並ぶ
    # (FrontmatterKeyInfo.sample_values の契約を引き継ぐ)。
    assert entry["observed_values_sample"] == ["active", "draft"]


def test_metadata_filter_diagnostics_multiple_keys(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """複数キーの AND で 0 件になったとき、全キーの diagnostics が並び、
    各キーの observed_values_sample は頻度降順 (同頻度は辞書順) で pin される."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    # status=active かつ priority=medium の組合せは存在しない (medium は draft のみ)
    res = idx.search("", metadata_filter={"status": "active", "priority": "medium"})
    assert res["total"] == 0
    diag = res["metadata_filter_diagnostics"]
    by_key = {entry["key"]: entry for entry in diag}
    assert set(by_key) == {"status", "priority"}
    # status: active (2) / draft (1) → 頻度降順で ["active", "draft"]
    assert by_key["status"]["key_present_in_index"] is True
    assert by_key["status"]["observed_values_sample"] == ["active", "draft"]
    # priority: high (1) / low (1) / medium (1) → 同頻度、辞書順 ["high", "low", "medium"]
    assert by_key["priority"]["key_present_in_index"] is True
    assert by_key["priority"]["observed_values_sample"] == ["high", "low", "medium"]


def test_metadata_filter_diagnostics_absent_when_results_non_empty(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """>0 件ヒット時は diagnostics を付けない (過剰なノイズを避ける)."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("", metadata_filter={"status": "active"})
    assert res["total"] > 0
    assert "metadata_filter_diagnostics" not in res


def test_metadata_filter_diagnostics_absent_without_metadata_filter(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """metadata_filter 未指定なら 0 件でも diagnostics は付けない."""
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("queryThatMatchesNothingZZZ")
    assert res["total"] == 0
    assert "metadata_filter_diagnostics" not in res


def test_metadata_filter_diagnostics_ne_operator(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """ne 演算で 0 件のときも diagnostics が付く.

    全ノートが同じ値を持つ場合、`{ne: X}` で 0 件になる。
    observed_values_sample が `[X]` だと「全件 X で絞れない」と気付ける。
    """
    _root, idx = vault_builder(
        {
            "a.md": "---\nstatus: active\n---\nbody\n",
            "b.md": "---\nstatus: active\n---\nbody\n",
        }
    )
    res = idx.search("", metadata_filter={"status": {"ne": "active"}})
    assert res["total"] == 0
    diag = res["metadata_filter_diagnostics"]
    assert len(diag) == 1
    assert diag[0]["key"] == "status"
    assert diag[0]["observed_values_sample"] == ["active"]


def test_metadata_filter_diagnostics_value_type_included(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """diagnostics に value_type を含める (Issue #80 cause #3: 型不一致の早期気付き).

    frontmatter は index 時に文字列に正規化されるため、bool や int の値を
    filter する際も ``"true"`` / ``"5"`` 形式で渡す必要がある。observed 値が
    ``"true"/"false"`` で value_type が ``"boolean"`` と判れば、エージェントが
    型不一致で silent miss しているケースを自己発見できる。
    """
    _root, idx = vault_builder(
        {
            "a.md": "---\narchived: true\n---\nbody\n",
            "b.md": "---\narchived: false\n---\nbody\n",
        }
    )
    # 意図的にヒットしない値を指定して 0 件を引く
    res = idx.search("", metadata_filter={"archived": "notabool"})
    assert res["total"] == 0
    diag = res["metadata_filter_diagnostics"]
    assert len(diag) == 1
    assert diag[0]["value_type"] == "boolean"


def test_metadata_filter_diagnostics_on_cache_hit(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """Tier 0 cache hit 経路でも diagnostics が付与される.

    初回呼び出しは Tier 2 (FTS5) で総件数 0 を cache に格納し、
    2 回目の同一クエリは Tier 0 で cache から返す。indexer.search() の
    cache hit 分岐でも ``_build_metadata_filter_diagnostics`` を通すことを
    lock-in し、将来 Tier 0/1 branch で silent に diagnostics が落ちた場合の
    regression を検知する。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    filt = {"status": "nonexistent_value"}
    first = idx.search("", metadata_filter=filt)
    assert first["total"] == 0
    assert first["tier"] == 2
    assert "metadata_filter_diagnostics" in first

    second = idx.search("", metadata_filter=filt)
    assert second["total"] == 0
    assert second["tier"] == 0, f"2 回目は Tier 0 cache hit になるはず (tier={second['tier']})"
    assert "metadata_filter_diagnostics" in second, (
        "Tier 0 cache hit 経路でも diagnostics が付与されるべき"
    )
    # 両 tier で同じ diagnostics を返す (cache は key_infos を参照しないので安定)
    assert second["metadata_filter_diagnostics"] == first["metadata_filter_diagnostics"]


def test_metadata_filter_diagnostics_with_non_empty_query(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """非空 FTS query + metadata_filter で 0 件のときも diagnostics が付く.

    FTS term + filter の積集合で 0 件になるパターン。indexer.search() の
    FTS 経路が filter 条件独立に diagnostics を emit することを確認する。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    # alpha/beta/gamma は本文に "obsidian" を含まないのでヒット 0、
    # かつ filter 条件も明示的に 0 件になるように指定
    res = idx.search("obsidian", metadata_filter={"status": "nonexistent_value"})
    assert res["total"] == 0
    assert "metadata_filter_diagnostics" in res
    diag = res["metadata_filter_diagnostics"]
    assert len(diag) == 1
    assert diag[0]["key"] == "status"
    assert diag[0]["observed_values_sample"] == ["active", "draft"]


# ---------------------------------------------------------------------------
# Issue #190: array 型 frontmatter の observed_values_sample を element-level に
# ---------------------------------------------------------------------------


def test_metadata_filter_diagnostics_array_sample_is_element_level(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """Issue #190: 配列型 frontmatter の diagnostics sample は単一要素の配列を返す.

    ``categories: [work, urgent]`` / ``[research]`` を持つ vault で
    ``{"categories": "nonexistent"}`` で検索すると 0 件になる。ここで
    ``observed_values_sample`` が JSON 配列文字列
    (``'["work", "urgent"]'``) ではなく個別要素 (``"work"`` / ``"urgent"`` /
    ``"research"``) の配列であることを pin する。エージェントが sample を
    そのまま filter value にコピペして retry できる UX の regression guard。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("", metadata_filter={"categories": "nonexistent"})
    assert res["total"] == 0
    diag = res["metadata_filter_diagnostics"]
    assert len(diag) == 1
    entry = diag[0]
    assert entry["key"] == "categories"
    assert entry["value_type"] == "array"

    samples = entry["observed_values_sample"]
    # 頻度降順 + 同頻度辞書順: research=1, urgent=1, work=1 → alphabetical
    assert samples == ["research", "urgent", "work"], (
        "配列型 diagnostics sample は単一要素ごとの頻度集計であるべき "
        f"(got {samples!r})"
    )

    # JSON-array 文字列が混入していないこと (以前の sample_values 契約流用バグの pin)
    for s in samples:
        assert not s.startswith("["), f"sample {s!r} が JSON 配列文字列になっている"


def test_metadata_filter_diagnostics_scalar_sample_unchanged(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """非配列型キー (string / number / boolean) は従来挙動を維持する (#190 の単一型不変性).

    配列要素展開ロジックは ``value_type == "array"`` のキーだけに適用し、
    scalar 型の observed_values_sample は ``FrontmatterKeyInfo.sample_values``
    そのままであること。
    """
    _root, idx = vault_builder(_META_FILTER_NOTES)
    res = idx.search("", metadata_filter={"status": "nonexistent"})
    assert res["total"] == 0
    diag = res["metadata_filter_diagnostics"]
    assert len(diag) == 1
    # 頻度降順: active=2, draft=1
    assert diag[0]["observed_values_sample"] == ["active", "draft"]


def test_metadata_filter_diagnostics_array_element_top5_frequency(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """配列型 diagnostics は頻度降順 top-5 (同頻度辞書順) の contract を守る."""
    _root, idx = vault_builder(
        {
            "a.md": "---\ntags: [x, y, z, a, b, c]\n---\nbody\n",
            "b.md": "---\ntags: [x, y]\n---\nbody\n",
            "c.md": "---\ntags: [x]\n---\nbody\n",
        }
    )
    res = idx.search("", metadata_filter={"tags": "nope"})
    assert res["total"] == 0
    samples = res["metadata_filter_diagnostics"][0]["observed_values_sample"]
    # freq: x=3, y=2, a=1, b=1, c=1, z=1 → top-5 は [x, y, a, b, c]
    assert samples == ["x", "y", "a", "b", "c"]
