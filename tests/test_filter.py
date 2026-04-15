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
# ne 配列型境界値テスト (Issue #43)
#
# 仕様まとめ:
#   - 空配列 []         : key が存在し要素が一切ない → ne にマッチ
#   - 1 要素一致        : 配列内に value が見つかる → 除外
#   - 多要素 (一致含む) : 1 つでも value を含めば → 除外
#   - 重複要素          : NOT EXISTS は重複の有無を問わず同じ → 除外
#   - キー missing      : IS NOT NULL ガードで除外 (eq と対称の 3 値論理)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fm, ne_value, should_match",
    [
        ({"tags": []}, "x", True),
        ({"tags": ["x"]}, "x", False),
        ({"tags": ["x", "y", "z"]}, "x", False),
        ({"tags": ["x", "x"]}, "x", False),
        ({"other": "y"}, "x", False),
    ],
    ids=[
        "empty-array",
        "single-element-match",
        "multi-element-has-match",
        "duplicate-elements",
        "missing-key",
    ],
)
def test_ne_array_boundary_values(
    conn: sqlite3.Connection,
    fm: dict[str, object],
    ne_value: str,
    should_match: bool,
) -> None:
    """ne 演算子の配列型境界値テスト (Issue #43).

    仕様:
    - 空配列はマッチ: key が存在し value を含まない → NOT EXISTS = True
    - 配列内に value が存在すれば除外 (1 要素 / 多要素 / 重複どれも同様)
    - キー missing は eq/ne 両方からマッチしない (3 値論理)
    """
    conn.execute("INSERT INTO notes VALUES (?, ?)", ("target.md", json.dumps(fm)))
    conn.commit()
    hits = _select(conn, MetadataCondition("tags", "ne", ne_value))
    if should_match:
        assert "target.md" in hits, f"target.md (fm={fm!r}) should match ne={ne_value!r}"
    else:
        assert "target.md" not in hits, f"target.md (fm={fm!r}) should not match ne={ne_value!r}"


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
