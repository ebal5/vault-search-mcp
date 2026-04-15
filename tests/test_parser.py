"""parser.py のテスト."""

from __future__ import annotations

from pathlib import Path

from vault_search.parser import ParsedNote, _normalize_fm, parse_note


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_parse_frontmatter_tags_as_list(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "a.md",
        "---\ntags:\n  - foo\n  - bar\n---\nbody\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.tags == ["foo", "bar"]
    assert note.path == "a.md"
    assert note.folder == ""


def test_parse_frontmatter_tags_as_string(tmp_path: Path) -> None:
    f = _write(tmp_path, "b.md", "---\ntags: foo, bar baz\n---\nbody\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert set(note.tags) >= {"foo", "bar", "baz"}


def test_inline_tags_cjk(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "c.md",
        "no frontmatter\nHere is an english #english-tag and a CJK #日本語タグ tag.\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert "english-tag" in note.tags
    assert "日本語タグ" in note.tags


def test_inline_tags_ignore_headings(tmp_path: Path) -> None:
    """見出しの # は tag として拾わない."""
    f = _write(tmp_path, "d.md", "# Heading Text\n\nbody #real-tag here\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert "real-tag" in note.tags
    # 'Heading' は tag に入らない (# の後にスペース)
    assert "Heading" not in note.tags


def test_aliases(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "e.md",
        "---\naliases:\n  - One\n  - Two\n---\nbody\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.aliases == ["One", "Two"]


def test_aliases_scalar_becomes_list(tmp_path: Path) -> None:
    f = _write(tmp_path, "e2.md", "---\naliases: Solo\n---\nbody\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.aliases == ["Solo"]


def test_timestamps_extraction(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "f.md",
        "---\ncreated_at: 2024-01-02\nmodified_at: 2024-03-04\n---\nbody\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.created_at == "2024-01-02"
    assert note.modified_at == "2024-03-04"


def test_timestamps_fallback_aliases(tmp_path: Path) -> None:
    """created / date / updated などのフォールバック."""
    f = _write(
        tmp_path,
        "g.md",
        "---\ndate: 2024-05-05\nupdated: 2024-06-06\n---\nbody\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.created_at == "2024-05-05"
    assert note.modified_at == "2024-06-06"


def test_missing_frontmatter_title_fallback(tmp_path: Path) -> None:
    f = _write(tmp_path, "my-note.md", "body only, no frontmatter.\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    # H1 が無いのでファイル名 stem
    assert note.title == "my-note"
    assert note.frontmatter == {}


def test_h1_title_extraction(tmp_path: Path) -> None:
    f = _write(tmp_path, "x.md", "# My H1 Title\n\nbody\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.title == "My H1 Title"


def test_malformed_frontmatter_resilient(tmp_path: Path) -> None:
    """壊れた YAML でも None にならず最大限抽出."""
    f = _write(
        tmp_path,
        "m.md",
        "---\ntitle: Broken\ntags: [unclosed\n---\nbody text\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    # body はきちんと本文のみ
    assert "body text" in note.content
    # title はフォールバックで拾えるか、H1 or ファイル名にフォールバック
    assert note.title in {"Broken", "m"}


def test_binary_file_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "bin.md"
    f.write_bytes(b"\xff\xfe\x00\x01not utf-8\x80")
    note = parse_note(f, tmp_path)
    assert note is None


def test_folder_relative_path(tmp_path: Path) -> None:
    f = _write(tmp_path, "sub/dir/nested.md", "# Nested\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.folder == "sub/dir"
    assert note.path == "sub/dir/nested.md"


def test_tags_json_roundtrip(tmp_path: Path) -> None:
    import json

    f = _write(tmp_path, "t.md", "---\ntags: [日本語, english]\n---\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    loaded = json.loads(note.tags_json)
    assert "日本語" in loaded


def test_frontmatter_and_inline_tags_merged_dedup(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "t2.md",
        "---\ntags: [foo]\n---\nbody has #foo again and #bar\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    # dedup: foo は1回だけ
    assert note.tags.count("foo") == 1
    assert "bar" in note.tags


def test_nonexistent_file_returns_none(tmp_path: Path) -> None:
    note = parse_note(tmp_path / "missing.md", tmp_path)
    assert note is None


# ---------------------------------------------------------------------------
# Scalar normalization (Issue #15 / #49)
#
# metadata_filter は常に str 値で比較するため、frontmatter の非 str 型を
# parse 時に文字列へ正規化する。これにより query-time の CAST が不要となり、
# bool "1"/"0" の UX ワートも解消される。
# ---------------------------------------------------------------------------


def test_frontmatter_int_normalized_to_string(tmp_path: Path) -> None:
    """YAML int (``priority: 5``) は frontmatter に ``"5"`` で格納される."""
    f = _write(tmp_path, "n.md", "---\npriority: 5\n---\nbody\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["priority"] == "5"


def test_frontmatter_bool_normalized_to_lowercase_string(tmp_path: Path) -> None:
    """YAML bool (``archived: true`` / ``active: false``) は ``"true"``/``"false"``."""
    f = _write(
        tmp_path,
        "n.md",
        "---\narchived: true\nactive: false\n---\nbody\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["archived"] == "true"
    assert note.frontmatter["active"] == "false"


def test_frontmatter_float_normalized_to_string(tmp_path: Path) -> None:
    """YAML float (``score: 4.5``) は ``"4.5"`` で格納される."""
    f = _write(tmp_path, "n.md", "---\nscore: 4.5\n---\nbody\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["score"] == "4.5"


def test_frontmatter_list_elements_normalized(tmp_path: Path) -> None:
    """list 内の int/bool も文字列化される."""
    f = _write(
        tmp_path,
        "n.md",
        "---\nlevels:\n  - 1\n  - 2\n  - 3\nflags:\n  - true\n  - false\n---\nbody\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["levels"] == ["1", "2", "3"]
    assert note.frontmatter["flags"] == ["true", "false"]


def test_frontmatter_date_normalized_to_iso_string(tmp_path: Path) -> None:
    """YAML date (``date: 2024-01-15``) は ISO 8601 文字列で格納される."""
    f = _write(tmp_path, "n.md", "---\ndate: 2024-01-15\n---\nbody\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["date"] == "2024-01-15"


def test_frontmatter_datetime_normalized_uses_isoformat_T_separator(
    tmp_path: Path,
) -> None:
    """YAML datetime (``due: 2024-01-15 14:30:00``) は ``T`` 区切りの ISO 8601.

    ``str(dt)`` は空白区切り ``"2024-01-15 14:30:00"`` を返すが、本関数は
    ``isoformat()`` を使うため ``"2024-01-15T14:30:00"`` になる。agent が
    date-only ``"2024-01-15"`` で filter すると silent miss するため、
    この実装契約を明示的に pin する (Reviewer C-R2 指摘)。
    """
    f = _write(tmp_path, "n.md", "---\ndue: 2024-01-15 14:30:00\n---\nbody\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["due"] == "2024-01-15T14:30:00"


def test_frontmatter_nested_dict_normalized_recursively(tmp_path: Path) -> None:
    """nested dict 内のスカラーも再帰的に正規化される (``_normalize_fm`` の dict 分岐)."""
    f = _write(
        tmp_path,
        "n.md",
        "---\nmeta:\n  sub: true\n  count: 5\n---\nbody\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["meta"] == {"sub": "true", "count": "5"}


def test_frontmatter_null_value_preserved_as_none(tmp_path: Path) -> None:
    """YAML null (``x: ~`` / ``x: null``) は None のまま格納.

    3 値論理のため ``{"x": "null"}`` のような文字列照合ではマッチしない
    (filter.py の IS NOT NULL ガードが drop)。不変条件の回帰ガード。
    """
    f = _write(tmp_path, "n.md", "---\narchived: null\nskip: ~\n---\nbody\n")
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["archived"] is None
    assert note.frontmatter["skip"] is None


def test_frontmatter_string_value_unchanged(tmp_path: Path) -> None:
    """str 値は副作用なしでそのまま格納される (回帰ガード)."""
    f = _write(
        tmp_path,
        "n.md",
        '---\ntitle: "Hello"\ntag_alias: foo\n---\nbody\n',
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["title"] == "Hello"
    assert note.frontmatter["tag_alias"] == "foo"


# ---------------------------------------------------------------------------
# Trust boundary & idempotency (Round 3 Reviewer C findings 2, 3)
#
# `ParsedNote.__post_init__` は parse_note を経由しない直接構築にも正規化を
# 強制する (Issue #15 / #49 の trust boundary)。これらのテストは
# ``_normalize_fm`` が str 素通し (冪等) であり、後続の型チェック / SQL 比較が
# 二重適用で壊れないことを pin する。
# ---------------------------------------------------------------------------


def test_direct_parsed_note_construction_normalizes_frontmatter() -> None:
    """``ParsedNote(..., frontmatter={...})`` 直接構築でも正規化が適用される.

    ``__post_init__`` が trust boundary を強制するガード。リファクタで
    post_init が消えても unit test で即座に検知する。
    """
    note = ParsedNote(
        path="x.md",
        title="x",
        folder="",
        content="",
        frontmatter={"priority": 5, "archived": True, "score": 4.5},
    )
    assert note.frontmatter["priority"] == "5"
    assert note.frontmatter["archived"] == "true"
    assert note.frontmatter["score"] == "4.5"


def test_normalize_fm_idempotent() -> None:
    """``_normalize_fm(_normalize_fm(x)) == _normalize_fm(x)`` (冪等性).

    ``_normalize_scalar`` は str を素通しするため、__post_init__ が
    正規化済み入力に二重適用されても壊れない。この性質が trust boundary の
    基盤なので、代表的なペイロードで固定化する。
    """
    payloads: list[object] = [
        {"x": True, "y": False},
        {"n": 5, "f": 4.5},
        {"nested": {"deep": {"flag": True, "count": 7}}},
        {"levels": [1, 2, 3], "flags": [True, False]},
        {"mixed": [{"sub": True}, {"n": 5}]},
        {"null_val": None, "str_val": "hello"},
    ]
    for p in payloads:
        once = _normalize_fm(p)
        twice = _normalize_fm(once)
        assert once == twice, f"idempotency broken for {p!r}: {once} != {twice}"


def test_frontmatter_dict_in_list_in_dict_normalized(tmp_path: Path) -> None:
    """dict → list → dict の深いネストも再帰正規化される.

    既存の ``test_frontmatter_nested_dict_normalized_recursively`` は
    dict-in-dict のみをカバー。list 内の dict 要素正規化は list 分岐の
    別コードパスのため個別にガードする (Round 3 Reviewer C finding 4)。
    """
    f = _write(
        tmp_path,
        "n.md",
        "---\nmeta:\n  - sub: true\n    count: 5\n  - sub: false\n    count: 3\n---\nbody\n",
    )
    note = parse_note(f, tmp_path)
    assert note is not None
    assert note.frontmatter["meta"] == [
        {"sub": "true", "count": "5"},
        {"sub": "false", "count": "3"},
    ]
