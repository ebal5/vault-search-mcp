"""Tests for vault_search.validation module.

This module validates adversarial inputs from AI agents. Agents may produce
hallucinated field names, path-traversal keys, or control-character payloads.
The validation layer rejects these early with actionable error messages.
"""

from __future__ import annotations

import pytest

from vault_search.validation import (
    ValidationError,
    validate_identifier,
    validate_value,
)

# ---------------------------------------------------------------------------
# validate_identifier — positive cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "status",
        "user_priority",
        "my-tag",
        "nested.key",
        "abc123",
        "A1_b2-c3.d4",
        "X",
    ],
)
def test_validate_identifier_accepts_valid(name: str) -> None:
    assert validate_identifier(name) == name


def test_validate_identifier_returns_input_verbatim() -> None:
    assert validate_identifier("priority") == "priority"


# ---------------------------------------------------------------------------
# validate_identifier — rejection cases
# ---------------------------------------------------------------------------


def test_validate_identifier_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        validate_identifier("")


@pytest.mark.parametrize(
    "name",
    [
        "foo\x00bar",
        "x\x1fy",
        "z\x7f",
        "a\x01b",
        "tab\there",
        "new\nline",
    ],
)
def test_validate_identifier_rejects_control_chars(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_identifier(name)


@pytest.mark.parametrize(
    "name",
    [
        "../etc",
        "/abs",
        "..\\win",
        "a/b",
    ],
)
def test_validate_identifier_rejects_path_traversal(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_identifier(name)


def test_validate_identifier_rejects_nul_byte() -> None:
    with pytest.raises(ValidationError):
        validate_identifier("x\x00y")


def test_validate_identifier_rejects_max_len_exceeded() -> None:
    too_long = "a" * 129
    with pytest.raises(ValidationError):
        validate_identifier(too_long)


def test_validate_identifier_accepts_max_len_boundary() -> None:
    exact = "a" * 128
    assert validate_identifier(exact) == exact


def test_validate_identifier_custom_max_len() -> None:
    with pytest.raises(ValidationError):
        validate_identifier("abcdef", max_len=5)
    assert validate_identifier("abcde", max_len=5) == "abcde"


@pytest.mark.parametrize(
    "name",
    [
        "has space",
        "with/slash",
        "with:colon",
        "quote'",
        "amp&",
        "percent%",
        "paren(",
        "bracket[",
    ],
)
def test_validate_identifier_rejects_disallowed_symbols(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_identifier(name)


@pytest.mark.parametrize(
    "name",
    [
        # Equivalence classes for empty dot-separated segments.
        # Keep representatives only; broader coverage (`a..`, `.a.b`,
        # `a.b.`, `a...b`) is subsumed by these classes.
        "..",  # dot-only (consecutive)
        ".",  # dot-only (single)
        ".a",  # leading empty
        "a.",  # trailing empty
        "a..b",  # consecutive empty between non-empty segments
    ],
)
def test_validate_identifier_rejects_malformed_dots(name: str) -> None:
    """Empty dot-segments expand to malformed SQLite JSON paths.

    See issue #14: inputs like ``a..b`` used to pass ``_IDENTIFIER_RE``
    and produced ``$.a..b`` which SQLite rejects with
    ``sqlite3.OperationalError``. They must raise ``ValidationError``.
    """
    with pytest.raises(ValidationError):
        validate_identifier(name)


@pytest.mark.parametrize(
    "name",
    [
        "a.b",  # minimum 2-segment
        "a.b.c",  # 3-segment
        "x_y.z-w",  # segments mixing _ and -
        "_._",  # single-char segments
        "1.2.3",  # numeric segments
    ],
)
def test_validate_identifier_accepts_dotted_paths(name: str) -> None:
    """Positive boundary for the segment-joined-by-dot grammar.

    Pairs with ``test_validate_identifier_rejects_malformed_dots`` so a
    future regex change that accidentally bans valid dotted identifiers
    is caught (silent regression on Obsidian nested-key support).
    """
    assert validate_identifier(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".",
        ".a",
        "a.",
        "a..b",
    ],
)
def test_validate_identifier_malformed_dot_message_names_the_cause(name: str) -> None:
    """Empty-segment errors must name the structural cause, not the chars.

    The previous message said ``"contains disallowed characters"`` for
    inputs like ``a..b`` even though every individual character is in
    the allowed set. That misleads agents into stripping characters
    instead of fixing the dot structure. The message must instead
    reference "empty"/"segment" so the agent can self-correct.
    """
    with pytest.raises(ValidationError) as exc:
        validate_identifier(name)
    msg = str(exc.value)
    # The new message must name the empty-segment cause...
    assert "empty" in msg and "segment" in msg, f"message should reference empty/segment: {msg!r}"
    # ...and must NOT mislead with the char-level "disallowed characters"
    # phrasing, which tricks agents into character-stripping retries.
    assert "disallowed characters" not in msg, (
        f"message should not blame chars for a structural error: {msg!r}"
    )


def test_validate_identifier_rejects_non_ascii_japanese() -> None:
    with pytest.raises(ValidationError):
        validate_identifier("重要")


def test_validate_identifier_rejects_non_ascii_latin() -> None:
    with pytest.raises(ValidationError):
        validate_identifier("café")


# ---------------------------------------------------------------------------
# validate_identifier — kind-aware error messages
# ---------------------------------------------------------------------------


def test_validate_identifier_error_message_includes_default_kind() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_identifier("bad name")
    assert "identifier" in str(exc.value)


def test_validate_identifier_error_message_includes_custom_kind() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_identifier("bad name", kind="field name")
    assert "field name" in str(exc.value)


def test_validate_identifier_error_message_includes_frontmatter_key_kind() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_identifier("../evil", kind="frontmatter key")
    assert "frontmatter key" in str(exc.value)


def test_validation_error_is_value_error_subclass() -> None:
    assert issubclass(ValidationError, ValueError)


# ---------------------------------------------------------------------------
# validate_value — positive cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "active",
        "high",
        "",
        "重要",
        "日本語テキスト",
        "v1.2.3",
        "hello world",
        "has/slash/is/fine",
        "quote'ok",
        "colon:ok",
        "café",
        "mix 123 abc",
    ],
)
def test_validate_value_accepts_valid(value: str) -> None:
    assert validate_value(value) == value


def test_validate_value_accepts_empty_string() -> None:
    assert validate_value("") == ""


def test_validate_value_accepts_max_len_boundary() -> None:
    exact = "x" * 1024
    assert validate_value(exact) == exact


# ---------------------------------------------------------------------------
# validate_value — rejection cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "foo\x00bar",
        "x\x1fy",
        "z\x7f",
        "a\x01b",
        "tab\there",
        "new\nline",
    ],
)
def test_validate_value_rejects_control_chars(value: str) -> None:
    with pytest.raises(ValidationError):
        validate_value(value)


def test_validate_value_rejects_max_len_exceeded() -> None:
    too_long = "x" * 1025
    with pytest.raises(ValidationError):
        validate_value(too_long)


def test_validate_value_custom_max_len() -> None:
    with pytest.raises(ValidationError):
        validate_value("abcdef", max_len=5)
    assert validate_value("abcde", max_len=5) == "abcde"


def test_validate_value_error_message_includes_default_kind() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_value("bad\x00value")
    assert "value" in str(exc.value)


def test_validate_value_error_message_includes_custom_kind() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_value("bad\x00value", kind="frontmatter value")
    assert "frontmatter value" in str(exc.value)
