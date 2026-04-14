"""build_sql_fragment の型強制 (Issue #15 / #49).

frontmatter に数値 (``priority: 5``) や bool (``archived: true``) が入っている
場合、``metadata_filter`` の値は常に文字列であるため SQLite 側で型不一致が
silent に起きていた:

- ``eq``: ``priority == "5"`` → int 5 と比較不成立で silent miss (#15)
- ``ne``: ``priority != "5"`` → int 5 と比較成立 (``5 != "5"``) で全件
  silent false positive (#49)

``build_sql_fragment`` を直接 SQLite against で実行し、実際に選択される行を
検証する。フラグメントは ``"AND ..."`` 形式で、``notes n`` 別名を前提とする。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from vault_search.filter import MetadataCondition, build_sql_fragment


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE notes (path TEXT PRIMARY KEY, frontmatter TEXT)")
    rows = [
        ("int-5.md", {"priority": 5}),
        ("int-3.md", {"priority": 3}),
        ("str-5.md", {"priority": "5"}),
        ("bool-true.md", {"archived": True}),
        ("bool-false.md", {"archived": False}),
        ("str-true.md", {"archived": "true"}),
        ("no-field.md", {"other": "x"}),
        ("arr-int.md", {"levels": [1, 2, 3]}),
        ("arr-str.md", {"levels": ["1", "2", "3"]}),
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
# Issue #15: eq silent miss for non-string frontmatter
# ---------------------------------------------------------------------------


def test_eq_int_frontmatter_matches_string_value(conn: sqlite3.Connection) -> None:
    """frontmatter が int の場合でも eq の文字列値でヒットする (型強制)."""
    cond = MetadataCondition(key="priority", op="eq", value="5")
    hits = _select(conn, cond)
    assert "int-5.md" in hits
    assert "str-5.md" in hits
    assert "int-3.md" not in hits


def test_eq_int_in_array_matches_string_value(conn: sqlite3.Connection) -> None:
    """配列フィールド内の int も文字列値とマッチする."""
    cond = MetadataCondition(key="levels", op="eq", value="2")
    hits = _select(conn, cond)
    assert "arr-int.md" in hits
    assert "arr-str.md" in hits


# ---------------------------------------------------------------------------
# Issue #49: ne silent false positive for non-string frontmatter
# ---------------------------------------------------------------------------


def test_ne_int_frontmatter_excludes_matching_string_value(
    conn: sqlite3.Connection,
) -> None:
    """frontmatter が int ``5`` のとき ``{"priority": {"ne": "5"}}`` は除外される."""
    cond = MetadataCondition(key="priority", op="ne", value="5")
    hits = _select(conn, cond)
    # int 5 と "5" は両方とも除外 — 型強制で同一視
    assert "int-5.md" not in hits
    assert "str-5.md" not in hits
    # int 3 は残る
    assert "int-3.md" in hits


def test_ne_int_array_excludes_matching_element(conn: sqlite3.Connection) -> None:
    """配列内に int 2 が含まれるなら ``ne: "2"`` はマッチ**しない**."""
    cond = MetadataCondition(key="levels", op="ne", value="2")
    hits = _select(conn, cond)
    # arr-int.md contains int 2 — excluded
    assert "arr-int.md" not in hits
    # arr-str.md contains "2" — excluded
    assert "arr-str.md" not in hits


# ---------------------------------------------------------------------------
# in 演算子も同様に型強制される
# ---------------------------------------------------------------------------


def test_in_int_frontmatter_matches_string_value(conn: sqlite3.Connection) -> None:
    cond = MetadataCondition(key="priority", op="in", value=("5", "3"))
    hits = _select(conn, cond)
    assert "int-5.md" in hits
    assert "int-3.md" in hits
    assert "str-5.md" in hits


def test_in_int_array_matches_string_value(conn: sqlite3.Connection) -> None:
    cond = MetadataCondition(key="levels", op="in", value=("2",))
    hits = _select(conn, cond)
    assert "arr-int.md" in hits
    assert "arr-str.md" in hits
