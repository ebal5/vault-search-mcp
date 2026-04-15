"""build_sql_fragment の SQL コントラクト (Issue #15 / #49 後).

parser.py が frontmatter スカラーを parse 時に str 正規化する前提のため、
``build_sql_fragment`` が生成する SQL は単純な str→str 比較のみを扱う。
本テストはその不変条件を in-memory SQLite で固定化する:

- eq / ne / in が str スカラー / str 配列に対して対称に動作
- キー欠落ノートは eq でマッチせず、ne でもマッチしない (3 値論理)
- 空配列・単一要素・重複要素の in 境界

int / bool / float / date の正規化は ``tests/test_parser.py`` と
``tests/test_indexer.py`` の end-to-end テストで検証する。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from vault_search.filter import MetadataCondition, build_sql_fragment, parse_metadata_filter
from vault_search.validation import ValidationError


@pytest.fixture
def conn() -> sqlite3.Connection:
    """parser 正規化後のストレージを模した str-only フィクスチャ."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE notes (path TEXT PRIMARY KEY, frontmatter TEXT)")
    rows: list[tuple[str, dict[str, object]]] = [
        ("hi.md", {"priority": "5"}),
        ("mid.md", {"priority": "3"}),
        ("flag-on.md", {"archived": "true"}),
        ("flag-off.md", {"archived": "false"}),
        ("no-key.md", {"other": "x"}),
        ("arr.md", {"levels": ["1", "2", "3"]}),
        ("arr-single.md", {"levels": ["1"]}),
        # Normalized bool array: YAML ``flags: [true, false]`` → ``["true", "false"]``.
        # ne / in の配列要素経路で bool 正規化後の str マッチが動くことを pin する。
        ("bool-arr.md", {"flags": ["true", "false"]}),
        ("bool-arr-true.md", {"flags": ["true"]}),
    ]
    for path, fm in rows:
        c.execute("INSERT INTO notes VALUES (?, ?)", (path, json.dumps(fm)))
    c.commit()
    return c


def _select(conn: sqlite3.Connection, cond: MetadataCondition) -> set[str]:
    fragment, params = build_sql_fragment(cond)
    sql = f"SELECT path FROM notes n WHERE 1=1 {fragment}"
    return {row[0] for row in conn.execute(sql, params).fetchall()}


# ---------------------------------------------------------------------------
# eq
# ---------------------------------------------------------------------------


def test_eq_scalar_str_match(conn: sqlite3.Connection) -> None:
    """str→str eq でマッチ."""
    hits = _select(conn, MetadataCondition("priority", "eq", "5"))
    assert hits == {"hi.md"}


def test_eq_array_element_match(conn: sqlite3.Connection) -> None:
    """配列フィールドは要素含有判定."""
    hits = _select(conn, MetadataCondition("levels", "eq", "2"))
    assert hits == {"arr.md"}


def test_eq_missing_key_excluded(conn: sqlite3.Connection) -> None:
    """キー欠落ノートは eq でマッチしない."""
    hits = _select(conn, MetadataCondition("priority", "eq", "5"))
    assert "no-key.md" not in hits


# ---------------------------------------------------------------------------
# ne
# ---------------------------------------------------------------------------


def test_ne_scalar_excludes_matching_value(conn: sqlite3.Connection) -> None:
    """ne は一致ノートを除外、他の値は残す."""
    hits = _select(conn, MetadataCondition("priority", "ne", "5"))
    assert hits == {"mid.md"}


def test_ne_bool_string_symmetric(conn: sqlite3.Connection) -> None:
    """bool 正規化後の "true"/"false" に対しても対称."""
    hits = _select(conn, MetadataCondition("archived", "ne", "true"))
    assert hits == {"flag-off.md"}


def test_ne_missing_key_excluded(conn: sqlite3.Connection) -> None:
    """キー欠落ノートは ne でもマッチしない (eq/ne 両方から除外、3 値論理)."""
    hits = _select(conn, MetadataCondition("priority", "ne", "5"))
    assert "no-key.md" not in hits


def test_ne_array_excludes_element_containing(conn: sqlite3.Connection) -> None:
    """配列内に value が含まれる場合 ne はマッチしない."""
    hits = _select(conn, MetadataCondition("levels", "ne", "2"))
    # arr.md は 2 を含む → 除外。arr-single.md は 1 のみ → マッチ
    assert hits == {"arr-single.md"}


# ---------------------------------------------------------------------------
# in
# ---------------------------------------------------------------------------


def test_in_scalar_any_match(conn: sqlite3.Connection) -> None:
    hits = _select(conn, MetadataCondition("priority", "in", ("5", "3")))
    assert hits == {"hi.md", "mid.md"}


def test_in_single_element(conn: sqlite3.Connection) -> None:
    """1 要素 tuple (placeholders 生成の fence-post ガード)."""
    hits = _select(conn, MetadataCondition("priority", "in", ("5",)))
    assert hits == {"hi.md"}


def test_in_array_any_element_match(conn: sqlite3.Connection) -> None:
    hits = _select(conn, MetadataCondition("levels", "in", ("2", "99")))
    assert hits == {"arr.md"}


# ---------------------------------------------------------------------------
# Normalized bool array element matching (Round 3 Reviewer C finding 5)
#
# YAML ``flags: [true, false]`` は parser で ``["true", "false"]`` へ
# 正規化される。ne / in 演算子の array-element path で bool 正規化文字列が
# 期待通り扱われることを固定化する。
# ---------------------------------------------------------------------------


def test_ne_bool_array_excludes_element_containing(conn: sqlite3.Connection) -> None:
    """配列内に "true" を含むノートは ne "true" でマッチしない."""
    hits = _select(conn, MetadataCondition("flags", "ne", "true"))
    # bool-arr.md, bool-arr-true.md はどちらも "true" を含む → 除外
    assert hits == set()


def test_in_bool_array_element_match(conn: sqlite3.Connection) -> None:
    """in 演算子が bool 正規化後の "false" を含む配列をヒットさせる."""
    hits = _select(conn, MetadataCondition("flags", "in", ("false",)))
    assert hits == {"bool-arr.md"}


# ---------------------------------------------------------------------------
# eq / ne partition property (Reviewer C2)
# ---------------------------------------------------------------------------


def test_eq_ne_exclude_null_value(conn: sqlite3.Connection) -> None:
    """YAML null で格納された値は eq / ne どちらにも含まれない (3 値論理).

    ``archived: null`` のノートを追加し、どちらの演算でもマッチしないことを
    ``IS NOT NULL`` ガードの回帰ガードとして固定化する。
    """
    conn.execute(
        "INSERT INTO notes VALUES (?, ?)",
        ("null-val.md", json.dumps({"archived": None})),
    )
    eq_hits = _select(conn, MetadataCondition("archived", "eq", "true"))
    ne_hits = _select(conn, MetadataCondition("archived", "ne", "true"))
    assert "null-val.md" not in eq_hits
    assert "null-val.md" not in ne_hits


def test_eq_ne_partition_on_present_key(conn: sqlite3.Connection) -> None:
    """キーが存在するノートに対し eq_hits と ne_hits は disjoint かつ和集合で
    キー保持ノート全体をカバーする。"""
    keyed_paths = {
        row[0]
        for row in conn.execute(
            "SELECT path FROM notes WHERE json_extract(frontmatter, '$.priority') IS NOT NULL"
        ).fetchall()
    }
    for value in ("5", "3", "99"):
        eq_hits = _select(conn, MetadataCondition("priority", "eq", value))
        ne_hits = _select(conn, MetadataCondition("priority", "ne", value))
        assert eq_hits.isdisjoint(ne_hits), f"eq and ne overlap for value={value!r}"
        assert eq_hits | ne_hits == keyed_paths, (
            f"eq ∪ ne != keyed_paths for value={value!r}: "
            f"eq={eq_hits} ne={ne_hits} keyed={keyed_paths}"
        )


def test_eq_ne_partition_on_array_key(conn: sqlite3.Connection) -> None:
    """配列キー (levels) に対しても eq/ne は disjoint かつ和集合でキー保持ノート全体をカバーする。

    配列 ne は ``NOT EXISTS(json_each ...)`` という非対称 SQL を使う。
    scalar ne と同じ partition property が成立することを固定化し、
    配列分岐の regression を防ぐ。
    """
    # arr.md: {"levels": ["1","2","3"]},  arr-single.md: {"levels": ["1"]}
    keyed_array = {"arr.md", "arr-single.md"}
    for value in ("1", "2", "99"):
        eq_hits = _select(conn, MetadataCondition("levels", "eq", value))
        ne_hits = _select(conn, MetadataCondition("levels", "ne", value))
        assert eq_hits.isdisjoint(ne_hits), f"eq and ne overlap for value={value!r}"
        assert eq_hits | ne_hits == keyed_array, (
            f"eq ∪ ne != keyed_array for value={value!r}: "
            f"eq={eq_hits} ne={ne_hits} keyed={keyed_array}"
        )


# ---------------------------------------------------------------------------
# Error message guidance (Round 3 Reviewer B finding)
#
# frontmatter スカラーは index 時に str 正規化されるため、agent が
# ``{"priority": 5}`` のように非 str 値を渡すと ValidationError になる。
# メッセージには単に「string が必要」と出すだけでなく、「stringify せよ」という
# 修正方針を含める (agent が "got int" だけだと auto-coerce を期待して
# retry するため)。
# ---------------------------------------------------------------------------


def test_implicit_eq_non_string_error_hints_stringification() -> None:
    """暗黙 eq に int を渡したエラーが stringify の指示を含む."""
    with pytest.raises(ValidationError) as exc:
        parse_metadata_filter({"priority": 5})
    msg = str(exc.value)
    assert "priority" in msg
    assert "int" in msg
    # 正規化の事実と修正方針を明示
    assert "normalized to strings" in msg
    assert '"5"' in msg  # 具体例


def test_in_operator_non_string_item_error_hints_stringification() -> None:
    """in のリスト要素に bool を渡したエラーも stringify を指示する."""
    with pytest.raises(ValidationError) as exc:
        parse_metadata_filter({"archived": {"in": [True]}})
    msg = str(exc.value)
    assert "bool" in msg
    assert "normalized to strings" in msg
    assert '"true"' in msg


def test_ne_operator_non_string_value_error_hints_stringification() -> None:
    """ne に float を渡したエラーも stringify を指示する."""
    with pytest.raises(ValidationError) as exc:
        parse_metadata_filter({"score": {"ne": 4.5}})
    msg = str(exc.value)
    assert "float" in msg
    assert "normalized to strings" in msg
    assert '"4.5"' in msg


# ---------------------------------------------------------------------------
# Negative tests: LLM-hallucinated operator forms (Issue #41)
#
# LLM が MongoDB 風演算子 ($in / $eq 等) や非サポート演算子を生成したとき、
# および in の引数に string を渡したときに ValidationError を送出することを
# spec として固定化する。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filter_input",
    [
        {"tags": {"$in": ["a", "b"]}},  # MongoDB 風 $ prefix
        {"tags": {"$eq": "x"}},
        {"tags": {"$ne": "x"}},
        {"tags": {"eq": "x"}},  # dict 形式の eq (bare string が正しい)
        {"tags": {"==": "x"}},  # 比較演算子記号
        {"tags": {"lt": 1}},  # 未サポート比較演算子
        {"tags": {"in": "a_string"}},  # in にリストでなく string を渡す
    ],
)
def test_unsupported_operator_raises_validation_error(filter_input: dict) -> None:
    """LLM がハルシネーションしがちな入力は ValidationError を送出する (Issue #41)."""
    with pytest.raises(ValidationError):
        parse_metadata_filter(filter_input)


@pytest.mark.parametrize(
    "filter_input",
    [
        {"tags": {"$in": ["a", "b"]}},
        {"tags": {"$eq": "x"}},
        {"tags": {"$ne": "x"}},
        {"tags": {"eq": "x"}},
        {"tags": {"==": "x"}},
        {"tags": {"lt": 1}},
    ],
)
def test_invalid_operator_hint_mentions_supported_forms(filter_input: dict) -> None:
    """未サポート演算子エラーのヒントに対応形式 (in / ne / bare string) が含まれる.

    エージェントがエラーメッセージを読んで自己修正できるよう、
    サポートされている構文 (ne / in / bare string) がヒントに明示されること。
    """
    with pytest.raises(ValidationError) as exc:
        parse_metadata_filter(filter_input)
    msg = str(exc.value)
    assert "in" in msg, f"'in' not found in error message: {msg}"
    assert "ne" in msg, f"'ne' not found in error message: {msg}"
    assert "string" in msg, f"'string' not found in error message: {msg}"


# ---------------------------------------------------------------------------
# Operator Literal type consolidation (Issue #13 b)
# ---------------------------------------------------------------------------


def test_operator_literal_exported_from_filter() -> None:
    """filter.py が Operator 型を公開し、eq/ne/in の 3 演算子を含むこと.

    単一 source of truth として Operator Literal を filter.py で定義し、
    他箇所が重複定義なしに参照できることを確認する。
    """
    import typing

    import vault_search.filter as filter_mod

    assert hasattr(filter_mod, "Operator"), (
        "filter.py must expose Operator Literal type as single source of truth for operators"
    )
    args = set(typing.get_args(filter_mod.Operator))
    assert args == {"eq", "ne", "in"}, f"Operator must cover exactly eq/ne/in: {args!r}"


def test_explicit_ops_derived_from_operator() -> None:
    """_EXPLICIT_OPS が Operator から eq を除いた派生であること."""
    import typing

    import vault_search.filter as filter_mod

    assert hasattr(filter_mod, "Operator"), "Operator must be defined to derive _EXPLICIT_OPS"
    assert hasattr(filter_mod, "_EXPLICIT_OPS"), "_EXPLICIT_OPS must be defined in filter.py"

    expected = frozenset(op for op in typing.get_args(filter_mod.Operator) if op != "eq")
    actual = frozenset(filter_mod._EXPLICIT_OPS)

    assert actual == expected, (
        f"_EXPLICIT_OPS {filter_mod._EXPLICIT_OPS!r} must match Operator-minus-eq {expected!r}"
    )
    assert "eq" not in filter_mod._EXPLICIT_OPS, "'eq' must not appear in _EXPLICIT_OPS"
