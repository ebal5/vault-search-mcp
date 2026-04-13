"""parser.py のテスト."""

from __future__ import annotations

from pathlib import Path

from vault_search.parser import parse_note


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
