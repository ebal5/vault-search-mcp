"""Issue #118 / #10: ``list_frontmatter_keys`` キャッシュ挙動の pin テスト.

設計: ``VaultIndex._query_frontmatter_keys_from_db()`` を分離し、
``list_frontmatter_keys()`` 外側でキャッシュする。
build_index / update_single / _upsert_note 等の書込み経路で invalidate する。

Issue #26: mutation test は SAMPLE_NOTES に依存せず、``vault_builder`` で
テスト専用の最小 vault を組む。SAMPLE_NOTES のキー所有ノートが変わっても
テストが壊れない独立性を持つ。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from vault_search.indexer import VaultIndex


def test_list_frontmatter_keys_uses_cache_on_repeated_calls(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """2 回目以降の呼出は DB スキャンを走らせない (cache 命中)."""
    _root, idx = vault_builder({"a.md": "---\nalpha: x\nbeta: y\n---\nbody\n"})
    first = idx.list_frontmatter_keys()
    assert first, "sanity: vault に frontmatter キーがあるはず"

    with patch.object(
        idx,
        "_query_frontmatter_keys_from_db",
        wraps=idx._query_frontmatter_keys_from_db,
    ) as spy:
        second = idx.list_frontmatter_keys()
        third = idx.list_frontmatter_keys()

    assert spy.call_count == 0, (
        f"cache 命中時は DB スキャンしないはず (call_count={spy.call_count})"
    )
    assert second == first
    assert third == first


def test_list_frontmatter_keys_invalidated_after_update_single_add(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """update_single でキーが追加されたら次の呼出で反映される."""
    root, idx = vault_builder({"seed.md": "---\nexisting_key: seed\n---\nbody\n"})
    before = {info.key for info in idx.list_frontmatter_keys()}
    assert "brand_new_key_xyzzy" not in before

    (root / "new_with_key.md").write_text(
        "---\nbrand_new_key_xyzzy: test\n---\nbody\n",
        encoding="utf-8",
    )
    assert idx.update_single("new_with_key.md") is True

    after = {info.key for info in idx.list_frontmatter_keys()}
    assert "brand_new_key_xyzzy" in after


def test_list_frontmatter_keys_invalidated_after_update_single_delete(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """最後のキー所有者が削除されたらキー集合から消える.

    ``shared_key`` を 2 ノートが共有。両方を削除すればキーは無くなる。
    """
    root, idx = vault_builder(
        {
            "alpha.md": "---\nshared_key: a\n---\nbody\n",
            "beta.md": "---\nshared_key: b\n---\nbody\n",
            "keepalive.md": "---\nunrelated_key: k\n---\nbody\n",
        }
    )
    _ = idx.list_frontmatter_keys()  # populate cache
    assert "shared_key" in {info.key for info in idx.list_frontmatter_keys()}

    (root / "alpha.md").unlink()
    (root / "beta.md").unlink()
    assert idx.update_single("alpha.md") is True
    assert idx.update_single("beta.md") is True

    after = {info.key for info in idx.list_frontmatter_keys()}
    assert "shared_key" not in after
    # 無関係キーは残る (部分削除 regression guard)
    assert "unrelated_key" in after


def test_list_frontmatter_keys_invalidated_after_build_index(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """build_index(force=True) 後も最新キー集合が返る."""
    root, idx = vault_builder({"seed.md": "---\nseed_key: s\n---\nbody\n"})
    _ = idx.list_frontmatter_keys()  # populate cache

    (root / "added.md").write_text(
        "---\nextra_key_from_rebuild: 1\n---\n",
        encoding="utf-8",
    )
    idx.build_index(force=True)

    keys = {info.key for info in idx.list_frontmatter_keys()}
    assert "extra_key_from_rebuild" in keys


def test_list_frontmatter_keys_concurrent_read_during_invalidate(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """read と invalidate の並行実行で TypeError / 破損が発生しない (Round 2 E1).

    `list_frontmatter_keys()` 読出し中に別スレッドが `_invalidate_caches()` で
    キャッシュを None にしても、snapshot pattern + lock 対称化により
    ``list(None)`` にならず、常に valid な list を返す。
    """
    import threading

    _root, idx = vault_builder(
        {
            "a.md": "---\nalpha: 1\n---\nbody\n",
            "b.md": "---\nbeta: 2\n---\nbody\n",
        }
    )
    idx.list_frontmatter_keys()  # prime cache

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def reader() -> None:
        barrier.wait()
        for _ in range(200):
            try:
                result = idx.list_frontmatter_keys()
                assert isinstance(result, list)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    def invalidator() -> None:
        barrier.wait()
        for _ in range(200):
            idx._invalidate_caches()

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=invalidator)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"concurrent read/invalidate should not raise: {errors!r}"
