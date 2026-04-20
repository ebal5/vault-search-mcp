"""Issue #20: FrontmatterKeyInfo モデルと list_frontmatter_keys 新仕様の失敗テスト.

Red フェーズ: src/ は無変更。全テストが AssertionError / TypeError / ValidationError で
明示的に失敗することを確認する (ImportError による collection error は回避済み)。

テスト件数: 12 件
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

try:
    from vault_search.schema_meta import FrontmatterKeyInfo
except ImportError:
    FrontmatterKeyInfo = None  # Red では未定義、Green で実装される

from vault_search.indexer import VaultIndex

# ---------------------------------------------------------------------------
# モデル系 (2 件)
# ---------------------------------------------------------------------------


def test_frontmatter_key_info_literal_and_forbid() -> None:
    """FrontmatterKeyInfo の Literal 制約と extra=forbid を検証する."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    # 正常構築
    obj = FrontmatterKeyInfo(key="x", value_type="mixed", note_count=1)
    assert obj.key == "x"
    assert obj.value_type == "mixed"
    assert obj.note_count == 1

    # Literal 制約: "datetime" は不正値 → Pydantic ValidationError
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        FrontmatterKeyInfo(key="x", value_type="datetime", note_count=1)

    # extra=forbid: 未知フィールドを渡すと ValidationError
    with pytest.raises(pydantic.ValidationError):
        FrontmatterKeyInfo(key="x", value_type="string", note_count=1, unknown_field="bad")


def test_frontmatter_key_info_sample_values_default() -> None:
    """sample_values は省略時に空リスト [] がデフォルト値になる."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    obj = FrontmatterKeyInfo(key="y", value_type="string", note_count=2)
    assert obj.sample_values == [], (
        f"sample_values のデフォルトは [] であるべき (got {obj.sample_values!r})"
    )


# ---------------------------------------------------------------------------
# 戻り型変更 / value_type 推論 (4 件)
# ---------------------------------------------------------------------------


def test_list_frontmatter_keys_returns_frontmatter_key_info(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """list_frontmatter_keys() が list[FrontmatterKeyInfo] を返す."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder({"note.md": "---\npriority: high\n---\nbody\n"})
    result = idx.list_frontmatter_keys()

    assert isinstance(result, list), f"戻り型は list であるべき (got {type(result)})"
    assert len(result) > 0, "frontmatter を持つ note があるのに空リストが返った"

    first = result[0]
    assert isinstance(first, FrontmatterKeyInfo), (
        f"各要素は FrontmatterKeyInfo であるべき (got {type(first)})"
    )
    assert isinstance(first.key, str), f".key は str であるべき (got {type(first.key)})"
    assert first.note_count >= 1, f".note_count >= 1 であるべき (got {first.note_count})"


def test_value_type_boolean_and_number_and_string(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """YAML bool/int/float は正規化後 value_type 推論で boolean/number に分類される."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder(
        {
            "bool_note.md": "---\ndone: true\n---\nbody\n",
            "int_note.md": "---\npriority: 5\n---\nbody\n",
            "float_note.md": "---\nscore: 4.5\n---\nbody\n",
            "str_note.md": "---\nstatus: active\n---\nbody\n",
        }
    )
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    assert "done" in key_map, "done キーが含まれるべき"
    assert key_map["done"].value_type == "boolean", (
        f"done: true → value_type='boolean' であるべき (got {key_map['done'].value_type!r})"
    )

    assert "priority" in key_map, "priority キーが含まれるべき"
    assert key_map["priority"].value_type == "number", (
        f"priority: 5 → value_type='number' であるべき (got {key_map['priority'].value_type!r})"
    )

    assert "score" in key_map, "score キーが含まれるべき"
    assert key_map["score"].value_type == "number", (
        f"score: 4.5 → value_type='number' であるべき (got {key_map['score'].value_type!r})"
    )

    assert "status" in key_map, "status キーが含まれるべき"
    assert key_map["status"].value_type == "string", (
        f"status: active → value_type='string' であるべき (got {key_map['status'].value_type!r})"
    )


def test_value_type_array(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """YAML list 値は value_type='array' に分類され、sample_values に JSON 文字列表現が入る."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder({"arr_note.md": "---\ntags:\n  - a\n  - b\n---\nbody\n"})
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    assert "tags" in key_map, "tags キーが含まれるべき"
    assert key_map["tags"].value_type == "array", (
        f"tags: [a, b] → value_type='array' であるべき (got {key_map['tags'].value_type!r})"
    )
    # sample_values に配列全体の JSON 文字列表現が厳密に含まれる (Reviewer C5)。
    # `str(list)` の Python repr (`"['a', 'b']"`) ではなく `json.dumps` 出力を pin。
    assert '["a", "b"]' in key_map["tags"].sample_values, (
        f'sample_values に JSON 配列文字列 \'["a", "b"]\' が含まれるべき '
        f"(got {key_map['tags'].sample_values!r})"
    )


def test_value_type_mixed(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """同一キーに string と number が混在する場合 value_type='mixed' になる."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder(
        {
            "level_str.md": "---\nlevel: high\n---\nbody\n",
            "level_num.md": "---\nlevel: 5\n---\nbody\n",
        }
    )
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    assert "level" in key_map, "level キーが含まれるべき"
    assert key_map["level"].value_type == "mixed", (
        "level が string と number で混在 → value_type='mixed' であるべき"
        f" (got {key_map['level'].value_type!r})"
    )


# ---------------------------------------------------------------------------
# sample_values / note_count 仕様 (3 件)
# ---------------------------------------------------------------------------


def test_sample_values_top5_frequency_and_tiebreak(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """sample_values は頻度降順 top-5、同頻度は辞書順で安定する."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    # status の頻度: active=3, draft=2, wip=1, todo=1, done=1, cancel=1
    _root, idx = vault_builder(
        {
            "a1.md": "---\nstatus: active\n---\nbody\n",
            "a2.md": "---\nstatus: active\n---\nbody\n",
            "a3.md": "---\nstatus: active\n---\nbody\n",
            "d1.md": "---\nstatus: draft\n---\nbody\n",
            "d2.md": "---\nstatus: draft\n---\nbody\n",
            "w1.md": "---\nstatus: wip\n---\nbody\n",
            "t1.md": "---\nstatus: todo\n---\nbody\n",
            "do1.md": "---\nstatus: done\n---\nbody\n",
            "c1.md": "---\nstatus: cancel\n---\nbody\n",
        }
    )
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    assert "status" in key_map, "status キーが含まれるべき"
    samples = key_map["status"].sample_values

    # 厳密に 5 件 (top-5 強制の regression guard: 全件返却バグや [:5] 削除バグを検知)
    assert len(samples) == 5, f"sample_values は厳密に 5 件 (got {len(samples)})"

    # 1-2 位は頻度上位: active(3), draft(2)
    assert samples[0] == "active", f"頻度 3 の 'active' が先頭 (got {samples[0]!r})"
    assert samples[1] == "draft", f"頻度 2 の 'draft' が 2 位 (got {samples[1]!r})"

    # 3-5 位は同頻度 (1) → 辞書順: cancel, done, todo (wip は 6 番目で除外)
    assert samples[2] == "cancel", f"3 位は辞書順 'cancel' (got {samples[2]!r})"
    assert samples[3] == "done", f"4 位は辞書順 'done' (got {samples[3]!r})"
    assert samples[4] == "todo", f"5 位は辞書順 'todo' (got {samples[4]!r})"
    assert "wip" not in samples, f"6 番目 (辞書順で 'wip') は除外されるべき (got {samples!r})"


def test_sample_values_excludes_empty_but_note_count_includes(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """空文字は sample_values から除外されるが note_count には含まれる."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder(
        {
            "empty_note.md": "---\nnote: ''\n---\nbody\n",
            "some_note.md": "---\nnote: something\n---\nbody\n",
        }
    )
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    assert "note" in key_map, "note キーが含まれるべき"
    info = key_map["note"]

    assert info.note_count == 2, (
        f"note_count は空文字ノートも含めて 2 であるべき (got {info.note_count})"
    )
    assert "" not in info.sample_values, (
        f"空文字は sample_values に含まれないべき (got {info.sample_values!r})"
    )
    assert "something" in info.sample_values, (
        f"'something' は sample_values に含まれるべき (got {info.sample_values!r})"
    )


def test_note_count_excludes_null(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """YAML null は note_count から除外される."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder(
        {
            "active_note.md": "---\nstatus: active\n---\nbody\n",
            "null_note.md": "---\nstatus: ~\n---\nbody\n",
        }
    )
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    assert "status" in key_map, "status キーが含まれるべき"
    assert key_map["status"].note_count == 1, (
        f"null は note_count に含まれないため 1 であるべき (got {key_map['status'].note_count})"
    )


# ---------------------------------------------------------------------------
# 追加観点 (Opus 指摘) (3 件)
# ---------------------------------------------------------------------------


def test_empty_vault_returns_empty_list(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """frontmatter を持つ note が 0 件の場合、空リストが返る."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder({"no_fm.md": "# Plain note\n\nNo frontmatter here.\n"})
    result = idx.list_frontmatter_keys()

    assert result == [], f"frontmatter なし vault では [] が返るべき (got {result!r})"


def test_dotted_nested_key_returns_key_info(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """ネスト frontmatter は親 dict key + dotted leaf key 両方が返る (#136 の既存契約保持)."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder({"nested.md": "---\nmeta:\n  author: foo\n---\nbody\n"})
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    # dotted leaf key は葉値の型で分類される
    assert "meta.author" in key_map, (
        f"ネスト key 'meta.author' が FrontmatterKeyInfo として含まれるべき (keys={list(key_map)})"
    )
    assert isinstance(key_map["meta.author"], FrontmatterKeyInfo)
    assert key_map["meta.author"].value_type == "string", (
        f"meta.author='foo' → value_type='string' (got {key_map['meta.author'].value_type!r})"
    )

    # 親 dict キー "meta" は value_type='object' として保持される (#136 既存契約 + filter 不可を明示)  # noqa: E501
    assert "meta" in key_map, (
        f"親 dict キー 'meta' も FrontmatterKeyInfo として含まれるべき (#136, keys={list(key_map)})"
    )
    assert key_map["meta"].value_type == "object", (
        f"親 dict キーは value_type='object' であるべき (got {key_map['meta'].value_type!r})"
    )
    # object 型の sample_values と note_count contract (Reviewer C3):
    # sample_values は空リスト (dict 値は filter 不可なのでサンプルを持たない)
    assert key_map["meta"].sample_values == [], (
        f"object 型キーの sample_values は空リスト (got {key_map['meta'].sample_values!r})"
    )
    # note_count は親キーが出現する note 数
    assert key_map["meta"].note_count == 1, (
        f"object 型キーの note_count は出現 note 数 (got {key_map['meta'].note_count})"
    )


def test_unicode_key_and_value_supported(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """Unicode キーと値が FrontmatterKeyInfo に正しく格納される."""
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder({"unicode_note.md": "---\n日本語キー: あいうえお\n---\nbody\n"})
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    assert "日本語キー" in key_map, (
        f"Unicode キー '日本語キー' が含まれるべき (keys={list(key_map)})"
    )
    assert "あいうえお" in key_map["日本語キー"].sample_values, (
        "sample_values に 'あいうえお' が含まれるべき"
        f" (got {key_map['日本語キー'].sample_values!r})"
    )


# ---------------------------------------------------------------------------
# Round 1 review 由来の追加 (A1 / B1)
# ---------------------------------------------------------------------------


def test_value_type_number_exponent_notation(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """YAML float の指数表記 (1e15 以上) が number と判定される (Reviewer A1).

    Python float の str 変換は 1e16 以上を ``'1e+16'`` 形式で出す。既存 regex
    ``^-?\\d+(\\.\\d+)?$`` はこれを string 扱いしていたため誤分類していた。
    """
    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder(
        {
            "exp_note.md": "---\nlarge: 1.5e100\n---\nbody\n",
            "neg_exp_note.md": "---\nsmall: 2e-10\n---\nbody\n",
        }
    )
    result = idx.list_frontmatter_keys()
    key_map = {item.key: item for item in result}

    assert "large" in key_map and key_map["large"].value_type == "number", (
        f"large: 1.5e100 → value_type='number' (got {key_map['large'].value_type!r})"
    )
    assert "small" in key_map and key_map["small"].value_type == "number", (
        f"small: 2e-10 → value_type='number' (got {key_map['small'].value_type!r})"
    )


def test_object_key_rejected_from_metadata_filter(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """親 dict key は metadata_filter の known_keys から除外される (B1).

    object 型は filter 不可なので、親キー名で filter を試みると silent 0 件ではなく
    UNKNOWN_FRONTMATTER_KEY ValidationError で agent に誤用を通知する。
    """
    from vault_search.exceptions import ValidationError

    assert FrontmatterKeyInfo is not None, "FrontmatterKeyInfo が schema_meta.py に未定義"

    _root, idx = vault_builder({"nested.md": "---\nmeta:\n  author: foo\n---\nbody\n"})
    # 親キー 'meta' は value_type='object' として list_frontmatter_keys に含まれるが、
    # metadata_filter の known_keys からは除外されている必要がある。
    with pytest.raises(ValidationError) as exc_info:
        idx.search(
            query="",
            folder=None,
            metadata_filter={"meta": "any"},
            limit=20,
            offset=0,
        )
    assert exc_info.value.error_code == "UNKNOWN_FRONTMATTER_KEY", (
        f"object 型キー 'meta' への filter は UNKNOWN_FRONTMATTER_KEY になるべき "
        f"(got {exc_info.value.error_code!r})"
    )

    # dotted leaf key 'meta.author' は引き続き filter 可能 (既存契約 #136 保持)
    res = idx.search(
        query="",
        folder=None,
        metadata_filter={"meta.author": "foo"},
        limit=20,
        offset=0,
    )
    assert res["total"] == 1


def test_object_key_filter_error_message_has_dotted_leaf_hint(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """object 型 filter エラーで dotted leaf key の hint が message にある (Round 2).

    Round 1 B1 fix で `meta` は UNKNOWN_FRONTMATTER_KEY を返すようになったが、
    schema://tools では `meta` が公開されているため、agent 視点では「schema と
    error が不整合」に見える。MCP wire では structured error 属性が消える
    (fastmcp-gotchas.md) ため、`str(err)` に agent が self-correct できるヒントを
    埋める必要がある。
    """
    from vault_search.exceptions import ValidationError

    _root, idx = vault_builder({"nested.md": "---\nmeta:\n  author: foo\n---\nbody\n"})

    with pytest.raises(ValidationError) as exc_info:
        idx.search(
            query="",
            folder=None,
            metadata_filter={"meta": "any"},
            limit=20,
            offset=0,
        )
    msg = str(exc_info.value)
    assert "meta" in msg, f"エラーメッセージに 'meta' が含まれるべき (got: {msg})"
    # "parent dict" / "dotted" / "object" いずれかの hint が含まれる
    hint_found = any(token in msg.lower() for token in ("parent dict", "dotted", "object", "leaf"))
    assert hint_found, (
        "エラーメッセージに dotted leaf / parent dict / object のいずれかの hint が"
        f" 必要 (got: {msg})"
    )


def test_object_key_multi_error_message_uses_plural_grammar(
    vault_builder: Callable[[dict[str, str]], tuple[Path, VaultIndex]],
) -> None:
    """複数の object 型キーを同時 filter 試行した時、plural 文法でメッセージが整う (Round 3).

    Round 2 E2 の hint は単数前提で書かれており、複数 object key の同時 filter で
    `'a', 'b' is a parent dict` という主語・動詞不一致 (plural subject + singular
    verb) が生じる。agent の batch hallucination は MCP 運用で主要シナリオなので
    文法整合性を pin する。
    """
    from vault_search.exceptions import ValidationError

    _root, idx = vault_builder(
        {
            "nested.md": "---\nmeta:\n  author: foo\nconfig:\n  env: prod\n---\nbody\n",
        }
    )

    with pytest.raises(ValidationError) as exc_info:
        idx.search(
            query="",
            folder=None,
            metadata_filter={"meta": "x", "config": "y"},
            limit=20,
            offset=0,
        )
    msg = str(exc_info.value)
    # 両 object key の言及
    assert "meta" in msg and "config" in msg, (
        f"エラーメッセージに両 object key が含まれる (got: {msg})"
    )
    # plural 形 ("are parent dicts" 等) で singular ("is a parent dict") を使わない
    assert "are parent dicts" in msg.lower() or "parent dicts" in msg.lower(), (
        f"複数 object key 時は plural 文法 ('are parent dicts' 等) であるべき (got: {msg})"
    )
    assert "is a parent dict" not in msg.lower(), (
        f"複数 object key 時に singular 'is a parent dict' は誤文法 (got: {msg})"
    )
