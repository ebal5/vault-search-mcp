"""Issue #118 / #10: ``list_frontmatter_keys`` キャッシュ挙動の pin テスト.

設計: ``VaultIndex._query_frontmatter_keys_from_db()`` を分離し、
``list_frontmatter_keys()`` 外側でキャッシュする。
build_index / update_single / _upsert_note 等の書込み経路で invalidate する。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from vault_search.indexer import VaultIndex


def test_list_frontmatter_keys_uses_cache_on_repeated_calls(
    vault_index: VaultIndex,
) -> None:
    """2 回目以降の呼出は DB スキャンを走らせない (cache 命中)."""
    first = vault_index.list_frontmatter_keys()
    assert first, "sanity: sample vault に frontmatter キーがあるはず"

    with patch.object(
        vault_index,
        "_query_frontmatter_keys_from_db",
        wraps=vault_index._query_frontmatter_keys_from_db,
    ) as spy:
        second = vault_index.list_frontmatter_keys()
        third = vault_index.list_frontmatter_keys()

    assert spy.call_count == 0, (
        f"cache 命中時は DB スキャンしないはず (call_count={spy.call_count})"
    )
    assert second == first
    assert third == first


def test_list_frontmatter_keys_invalidated_after_update_single_add(
    vault_index: VaultIndex, tmp_vault: Path
) -> None:
    """update_single でキーが追加されたら次の呼出で反映される."""
    before = set(vault_index.list_frontmatter_keys())
    assert "brand_new_key_xyzzy" not in before

    (tmp_vault / "new_with_key.md").write_text(
        "---\nbrand_new_key_xyzzy: test\n---\nbody\n",
        encoding="utf-8",
    )
    assert vault_index.update_single("new_with_key.md") is True

    after = set(vault_index.list_frontmatter_keys())
    assert "brand_new_key_xyzzy" in after


def test_list_frontmatter_keys_invalidated_after_update_single_delete(
    vault_index: VaultIndex, tmp_vault: Path
) -> None:
    """最後のキー所有者が削除されたらキー集合から消える.

    sample vault の ``categories`` は Welcome.md と Research/alpha.md が持つ。
    両方を削除すればキーは無くなる。
    """
    _ = vault_index.list_frontmatter_keys()  # populate cache
    assert "categories" in set(vault_index.list_frontmatter_keys())

    (tmp_vault / "Welcome.md").unlink()
    (tmp_vault / "Research" / "alpha.md").unlink()
    assert vault_index.update_single("Welcome.md") is True
    assert vault_index.update_single("Research/alpha.md") is True

    after = set(vault_index.list_frontmatter_keys())
    assert "categories" not in after


def test_list_frontmatter_keys_invalidated_after_build_index(
    tmp_vault: Path, tmp_path: Path
) -> None:
    """build_index(force=True) 後も最新キー集合が返る."""
    db_path = tmp_path / "test.db"
    idx = VaultIndex(tmp_vault, db_path=db_path)
    idx.build_index()
    _ = idx.list_frontmatter_keys()  # populate cache

    (tmp_vault / "added.md").write_text(
        "---\nextra_key_from_rebuild: 1\n---\n",
        encoding="utf-8",
    )
    idx.build_index(force=True)

    keys = set(idx.list_frontmatter_keys())
    assert "extra_key_from_rebuild" in keys
