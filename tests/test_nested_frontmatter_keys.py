"""Issue #136: ネスト frontmatter のキーが dotted 形式で known_keys に現れること.

``validate_identifier`` は ``a.b`` 形式を許可し、SQL (``$.meta.author``) も
正しく辿れるのに、``list_frontmatter_keys`` がトップレベルキーしか返さないため
``metadata_filter={"meta.author": ...}`` が UNKNOWN_FRONTMATTER_KEY で拒否される
false positive を pin する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vault_search.indexer import VaultIndex
from vault_search.validation import ValidationError


@pytest.fixture
def nested_vault(tmp_path: Path) -> Path:
    """ネスト frontmatter を持つ最小 vault."""
    root = tmp_path / "vault"
    root.mkdir()
    (root / "nested.md").write_text(
        "---\nmeta:\n  author: foo\n  tag: bar\nsimple: scalar-value\n---\nbody\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def nested_index(nested_vault: Path, tmp_path: Path) -> VaultIndex:
    db_path = tmp_path / "nested.db"
    idx = VaultIndex(nested_vault, db_path=db_path)
    idx.build_index()
    return idx


def test_list_frontmatter_keys_includes_nested_dotted_keys(
    nested_index: VaultIndex,
) -> None:
    """ネスト dict 値は dotted key として known_keys に現れる."""
    keys = set(nested_index.list_frontmatter_keys())
    assert "meta" in keys, "トップレベルキーも従来通り含まれる"
    assert "meta.author" in keys, "ネスト子キーも dotted で含まれる必要がある (#136)"
    assert "meta.tag" in keys
    assert "simple" in keys


def test_vault_search_accepts_nested_metadata_filter_key(
    nested_index: VaultIndex,
) -> None:
    """nested frontmatter のキーが metadata_filter で false positive UNKNOWN されない.

    ``meta.author`` は validate_identifier で許容され SQL も辿れるので、
    unknown エラーにならず 1 件 hit するべき。
    """
    res = nested_index.search(
        query="",
        folder=None,
        metadata_filter={"meta.author": "foo"},
        limit=20,
        offset=0,
    )
    assert res["total"] == 1
    assert res["results"][0]["path"] == "nested.md"


def test_vault_search_rejects_truly_unknown_dotted_key(
    nested_index: VaultIndex,
) -> None:
    """存在しない親キーを使った dotted key は引き続き UNKNOWN として拒否される.

    #136 修正で「ドット以降を無条件に pass させる」弱い修正を選ばないことを pin。
    (Option A: 再帰展開で known_keys に実在する dotted のみ追加する方針)
    """
    with pytest.raises(ValidationError) as exc_info:
        nested_index.search(
            query="",
            folder=None,
            metadata_filter={"nonexistent.child": "value"},
            limit=20,
            offset=0,
        )
    err = exc_info.value
    assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"
