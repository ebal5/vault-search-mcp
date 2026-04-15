"""共通フィクスチャ: tmp_vault / vault_index."""

from __future__ import annotations

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
