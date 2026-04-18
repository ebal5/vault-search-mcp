"""共通フィクスチャ: tmp_vault / vault_index / vault_builder.

Issue #26: 新規テストでは ``vault_builder`` ファクトリを使ってテスト専用の
最小ノート集合で独立した vault を構築すること。``tmp_vault`` / ``vault_index``
の共有サンプル (SAMPLE_NOTES) は広範なテストで再利用されているため、
内容を変更すると連鎖的に失敗する。目的別の最小 vault を組む方が影響範囲を
局所化できる。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from vault_search.indexer import VaultIndex

SAMPLE_NOTES: dict[str, str] = {
    # ルート直下の英語ノート
    "Welcome.md": (
        "---\n"
        "title: Welcome\n"
        "tags:\n"
        "  - intro\n"
        "  - getting-started\n"
        "aliases:\n"
        "  - Hello\n"
        "  - Intro\n"
        "created_at: 2024-01-01\n"
        "modified_at: 2024-02-01\n"
        "status: active\n"
        "priority: high\n"
        "categories:\n"
        "  - work\n"
        "  - urgent\n"
        "---\n"
        "# Welcome\n"
        "\n"
        "This note introduces the vault. It mentions #extra-tag inline.\n"
        "Obsidian is a knowledge base. Search finds the word obsidian.\n"
    ),
    # 日本語 + CJK タグ
    "Projects/日本語ノート.md": (
        "---\n"
        "title: 日本語ノート\n"
        "tags: [project/alpha, status/wip]\n"
        "status: draft\n"
        "priority: medium\n"
        "---\n"
        "これは日本語のノートです。 #日本語タグ を含みます。\n"
        "本文には検索用の文字列「あいうえお」も含まれます。\n"
    ),
    # frontmatter なし
    "Projects/plain.md": (
        "# Plain Note\n\nNo frontmatter here. Just some content about testing.\n"
    ),
    # 壊れた frontmatter — fallback パーサーで拾われる想定
    "malformed.md": (
        "---\ntitle: Malformed\ntags: [unclosed\n---\nBody after malformed frontmatter.\n"
    ),
    # タグ付き別フォルダ
    "Research/alpha.md": (
        "---\n"
        "tags:\n"
        "  - research\n"
        "  - project/alpha\n"
        "status: active\n"
        "priority: low\n"
        "categories:\n"
        "  - research\n"
        "---\n"
        "Research notes about obsidian usage patterns.\n"
    ),
    # folder prefix テスト用マーカーノート (Projects 配下)
    # test_folder_prefix_does_not_match_sibling が vacuous pass にならないよう
    # "obsidian-marker-projects" というユニークトークンで positively ヒットさせる
    "Projects/marker.md": (
        "---\ntitle: Projects Marker\n---\n"
        "obsidian-marker-projects unique token for folder prefix test.\n"
    ),
    # 除外対象: `_` プレフィックスフォルダ (root level)
    "_archive/old.md": ("---\ntags: [archived]\n---\nArchived content about obsidian.\n"),
    # 除外対象: `.` プレフィックスフォルダ
    ".trash/deleted.md": "# Trashed\n\nShould not be indexed.\n",
}


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """サンプルノート入りの一時 Vault を作成."""
    root = tmp_path / "vault"
    root.mkdir()
    for rel, body in SAMPLE_NOTES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


@pytest.fixture
def vault_index(tmp_vault: Path, tmp_path: Path) -> VaultIndex:
    """tmp_vault に対する構築済み VaultIndex."""
    db_path = tmp_path / "test.db"
    idx = VaultIndex(tmp_vault, db_path=db_path)
    idx.build_index()
    return idx


@pytest.fixture
def vault_builder(tmp_path: Path) -> Callable[[dict[str, str]], tuple[Path, VaultIndex]]:
    """独立した最小 vault を build するファクトリ (Issue #26).

    各テストが必要最小限のノートだけを含む vault を作れるようにし、
    SAMPLE_NOTES 共有による coupling を断つ。DB は tmp_path 下で自動採番するので
    同じテスト内で複数の vault を独立に build できる。

    戻り値は ``(vault_root, built_index)`` のタプル。

    例::

        def test_my_filter(vault_builder):
            _root, idx = vault_builder({
                "a.md": "---\\nstatus: active\\n---\\nbody\\n",
                "b.md": "---\\nstatus: draft\\n---\\nbody\\n",
            })
            res = idx.search("", metadata_filter={"status": "active"})
            assert {r["path"] for r in res["results"]} == {"a.md"}
    """
    counter = 0

    def _build(notes: dict[str, str]) -> tuple[Path, VaultIndex]:
        nonlocal counter
        counter += 1
        root = tmp_path / f"vault_{counter}"
        root.mkdir()
        for rel, body in notes.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        idx = VaultIndex(root, db_path=tmp_path / f"vault_{counter}.db")
        idx.build_index()
        return root, idx

    return _build
