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

from vault_search.filter import MetadataCondition, build_sql_fragment


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
