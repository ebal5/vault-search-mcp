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


# ---------------------------------------------------------------------------
# ValidationError.unknown_keys 契約 (Issue #123 round 1 review D5)
#
# ``unknown_keys`` 属性は ``parse_metadata_filter`` の batch 経路専用だが、
# ValidationError の契約は validation 層の単体テストとして独立に pin する。
# 将来 subclass 化 (#32 follow-up) や属性名変更時の source of truth。
# ---------------------------------------------------------------------------


class TestValidationErrorUnknownKeysAttribute:
    """ValidationError.unknown_keys 属性の直接契約テスト."""

    def test_default_is_empty_dict(self) -> None:
        """unknown_keys を渡さないと空 dict になる."""
        err = ValidationError("msg")
        assert err.unknown_keys == {}

    def test_mapping_converted_to_dict_of_tuple(self) -> None:
        """渡した Mapping は ``dict[str, tuple[str, ...]]`` に正規化される."""
        err = ValidationError(
            "msg",
            unknown_keys={"priorty": ["priority"], "statu": ["status"]},
        )
        assert isinstance(err.unknown_keys, dict)
        assert err.unknown_keys["priorty"] == ("priority",)
        assert err.unknown_keys["statu"] == ("status",)

    def test_empty_sequence_preserved_per_key(self) -> None:
        """候補なしキーは空 tuple として保持される (None でなく空 tuple)."""
        err = ValidationError("msg", unknown_keys={"foo": []})
        assert err.unknown_keys == {"foo": ()}

    def test_non_unknown_key_error_has_empty_unknown_keys(self) -> None:
        """他 error_code の ValidationError は unknown_keys が常に空."""
        err = ValidationError("bad op", error_code="VALIDATION_ERROR")
        assert err.unknown_keys == {}


# ---------------------------------------------------------------------------
# validate_known_keys (batch) + format_unknown_keys_message — Issues #138/#140/#141
#
# filter.py の parse_metadata_filter から unknown key 検出を抽出した batch
# validator。ValidationError(error_code="UNKNOWN_FRONTMATTER_KEY") を
# did_you_mean (difflib) + allowed (sorted) + unknown_keys (per-key 候補) 付きで
# 送出する。schema_ref / registry_label 引数で MCP/frontmatter 固有文言を inject。
# ---------------------------------------------------------------------------


class TestValidateKnownKeysBatch:
    """validate_known_keys (batch API) の契約テスト (#141)."""

    def test_all_known_names_no_op(self) -> None:
        """全 name が known_keys に含まれる場合、例外は送出されない."""
        from vault_search.validation import validate_known_keys

        validate_known_keys(
            ["priority", "status"], ["priority", "status", "tags"], kind="frontmatter key"
        )

    def test_empty_names_no_op_even_with_empty_known_keys(self) -> None:
        """names が空なら known_keys も空でも no-op (空 dict frontmatter への配慮)."""
        from vault_search.validation import validate_known_keys

        validate_known_keys([], [], kind="frontmatter key")

    def test_single_unknown_raises_with_single_key_shape(self) -> None:
        """単一 unknown でも batch と同じ ValidationError shape で返る.

        error_code / did_you_mean / allowed / unknown_keys 属性を検証する。
        """
        from vault_search.validation import validate_known_keys

        with pytest.raises(ValidationError) as exc:
            validate_known_keys(["priorty"], ["priority", "status"], kind="frontmatter key")
        err = exc.value
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"
        assert err.did_you_mean == ("priority",)
        assert tuple(sorted(err.allowed)) == ("priority", "status")
        assert err.unknown_keys == {"priorty": ("priority",)}

    def test_multiple_unknown_batched_and_sorted(self) -> None:
        """複数 unknown は 1 回の error にまとめられ alphabetical に整列される."""
        from vault_search.validation import validate_known_keys

        with pytest.raises(ValidationError) as exc:
            validate_known_keys(["zapple", "aapple"], ["banana"], kind="frontmatter key")
        err = exc.value
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"
        # insertion order に依存せず alphabetical で固定
        assert list(err.unknown_keys.keys()) == ["aapple", "zapple"]

    def test_mixed_known_unknown_only_unknown_reported(self) -> None:
        """known と unknown の混在で unknown のみ報告される."""
        from vault_search.validation import validate_known_keys

        with pytest.raises(ValidationError) as exc:
            validate_known_keys(
                ["priority", "typo", "status"],
                ["priority", "status", "tags"],
                kind="frontmatter key",
            )
        err = exc.value
        assert set(err.unknown_keys.keys()) == {"typo"}

    def test_no_close_match_yields_empty_suggestions(self) -> None:
        """close match が無いキーは空 tuple suggestion を持つ."""
        from vault_search.validation import validate_known_keys

        with pytest.raises(ValidationError) as exc:
            validate_known_keys(["xyzqqq"], ["priority", "status"], kind="frontmatter key")
        err = exc.value
        assert err.unknown_keys == {"xyzqqq": ()}
        assert err.did_you_mean == ()

    def test_schema_ref_and_registry_label_injected_into_message(self) -> None:
        """MCP/frontmatter 固有文言が引数で注入される (#138).

        近似マッチがある入力 (did-you-mean 経路) を使うのは、候補なし経路だと
        "for the full list" という kind-agnostic fallback 文言が挟まり
        ``registry_label`` が使われないため (bit-identical 維持の副作用)。
        """
        from vault_search.validation import validate_known_keys

        with pytest.raises(ValidationError) as exc:
            validate_known_keys(
                ["priorty"],
                ["priority"],
                kind="custom kind",
                schema_ref="config://alt",
                registry_label="custom_list",
            )
        msg = str(exc.value)
        assert "config://alt" in msg, f"schema_ref must be injected; got {msg!r}"
        assert "custom_list" in msg, f"registry_label must be injected; got {msg!r}"
        # デフォルト値がリークしない
        assert "schema://tools" not in msg
        assert "frontmatter_keys" not in msg

    def test_default_schema_ref_and_registry_label_match_legacy_messages(self) -> None:
        """デフォルト値で従来 message が bit-identical で生成される (#138/#140)."""
        from vault_search.validation import validate_known_keys

        with pytest.raises(ValidationError) as exc:
            validate_known_keys(["priorty"], ["priority", "status"], kind="frontmatter key")
        expected = (
            "Unknown frontmatter key 'priorty'; "
            "did you mean: priority? "
            "See schema://tools for the frontmatter_keys list"
        )
        assert str(exc.value) == expected


class TestFormatUnknownKeysMessage:
    """統一 message builder (format_unknown_keys_message) の契約 (#140)."""

    def test_single_key_with_suggestions_bit_identical(self) -> None:
        """1 key with suggestions は従来 format_unknown_key_message と bit-identical."""
        from vault_search.validation import format_unknown_keys_message

        msg = format_unknown_keys_message(
            {"priorty": ("priority",)},
            "frontmatter key",
            ["priority", "status"],
        )
        assert msg == (
            "Unknown frontmatter key 'priorty'; "
            "did you mean: priority? "
            "See schema://tools for the frontmatter_keys list"
        )

    def test_single_key_no_suggestions_bit_identical(self) -> None:
        """1 key without suggestions は従来 "full list" 文言を維持 (bit-identical)."""
        from vault_search.validation import format_unknown_keys_message

        msg = format_unknown_keys_message(
            {"xyz": ()},
            "frontmatter key",
            ["a", "b", "c"],
        )
        assert msg == (
            "Unknown frontmatter key 'xyz'; "
            "valid keys include: a, b, c. "
            "See schema://tools for the full list"
        )

    def test_multi_key_all_suggestions_bit_identical(self) -> None:
        """複数 key 全候補ありは従来 _raise_unknown_keys と bit-identical."""
        from vault_search.validation import format_unknown_keys_message

        msg = format_unknown_keys_message(
            {"priorty": ("priority",), "statu": ("status",)},
            "frontmatter key",
            ["priority", "status"],
        )
        assert msg == (
            "Unknown frontmatter keys: "
            "'priorty' (did you mean: priority), "
            "'statu' (did you mean: status). "
            "See schema://tools for the frontmatter_keys list"
        )

    def test_multi_key_mixed_suggestions_bit_identical(self) -> None:
        """複数 key 混在 (候補あり + なし) は valid keys preview 付き."""
        from vault_search.validation import format_unknown_keys_message

        msg = format_unknown_keys_message(
            {"priorty": ("priority",), "foo": ()},
            "frontmatter key",
            ["priority", "status"],
        )
        assert msg == (
            "Unknown frontmatter keys: "
            "'foo' (no close match), "
            "'priorty' (did you mean: priority). "
            "Valid keys include: priority, status. "
            "See schema://tools for the frontmatter_keys list"
        )

    def test_multi_key_all_no_suggestions_bit_identical(self) -> None:
        """複数 key 全候補なしは valid keys preview 付き."""
        from vault_search.validation import format_unknown_keys_message

        msg = format_unknown_keys_message(
            {"foo": (), "bar": ()},
            "frontmatter key",
            ["priority", "status"],
        )
        assert msg == (
            "Unknown frontmatter keys: "
            "'bar' (no close match), "
            "'foo' (no close match). "
            "Valid keys include: priority, status. "
            "See schema://tools for the frontmatter_keys list"
        )

    def test_preview_truncates_to_five_entries(self) -> None:
        """6 件以上の known_keys は 5 件 preview + ', ...' 末尾で truncate される."""
        from vault_search.validation import format_unknown_keys_message

        msg = format_unknown_keys_message(
            {"xyz": ()},
            "frontmatter key",
            ["a", "b", "c", "d", "e", "f", "g"],
        )
        assert "a, b, c, d, e, ..." in msg
