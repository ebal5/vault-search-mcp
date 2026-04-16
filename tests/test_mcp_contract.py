"""mcp_contract 単体の invariant pin.

schema://tools リソースや MCP tools/list outputSchema の結合テストは
``test_schema_resource.py`` / ``test_mcp_wrapping.py`` が担う。
ここは mcp_contract.py 内の cross-module 不変条件 (単一ソース真実性) だけを pin する。
"""

from __future__ import annotations


def test_limit_input_schema_maximum_matches_limit_max() -> None:
    """_LIMIT_INPUT_SCHEMA の maximum が validation.LIMIT_MAX と同値であること."""
    from vault_search.mcp_contract import _LIMIT_INPUT_SCHEMA
    from vault_search.validation import LIMIT_MAX

    assert _LIMIT_INPUT_SCHEMA["maximum"] == LIMIT_MAX


def test_limit_input_schema_description_references_limit_max() -> None:
    """description 文字列中の数値も LIMIT_MAX から展開されていること (stale 防止)."""
    from vault_search.mcp_contract import _LIMIT_INPUT_SCHEMA
    from vault_search.validation import LIMIT_MAX

    assert str(LIMIT_MAX) in _LIMIT_INPUT_SCHEMA["description"]


# ---------------------------------------------------------------------------
# Issue #75: propertyNames constraint
# ---------------------------------------------------------------------------


def test_identifier_json_pattern_exported() -> None:
    """IDENTIFIER_JSON_PATTERN が validation モジュールから import 可能で非空文字列であること."""
    from vault_search.validation import IDENTIFIER_JSON_PATTERN  # type: ignore[attr-defined]

    assert isinstance(IDENTIFIER_JSON_PATTERN, str)
    assert len(IDENTIFIER_JSON_PATTERN) > 0


def test_identifier_max_len_exported() -> None:
    """IDENTIFIER_MAX_LEN が validation モジュールから import 可能で 128 であること."""
    from vault_search.validation import IDENTIFIER_MAX_LEN  # type: ignore[attr-defined]

    assert IDENTIFIER_MAX_LEN == 128


def test_metadata_filter_has_property_names() -> None:
    """metadata_filter スキーマに propertyNames キーが存在すること (Issue #75)."""
    from vault_search.mcp_contract import TOOL_SPECS

    mf_schema = TOOL_SPECS["vault_search"].input_schema["properties"]["metadata_filter"]
    assert "propertyNames" in mf_schema


def test_property_names_pattern_matches_validation() -> None:
    """propertyNames["pattern"] が validation.IDENTIFIER_JSON_PATTERN と同値であること (単一ソース真実性)."""
    from vault_search.mcp_contract import TOOL_SPECS
    from vault_search.validation import IDENTIFIER_JSON_PATTERN  # type: ignore[attr-defined]

    mf_schema = TOOL_SPECS["vault_search"].input_schema["properties"]["metadata_filter"]
    assert mf_schema["propertyNames"]["pattern"] == IDENTIFIER_JSON_PATTERN


def test_property_names_max_length_matches_validation() -> None:
    """propertyNames["maxLength"] が validation.IDENTIFIER_MAX_LEN と同値であること (単一ソース真実性)."""
    from vault_search.mcp_contract import TOOL_SPECS
    from vault_search.validation import IDENTIFIER_MAX_LEN  # type: ignore[attr-defined]

    mf_schema = TOOL_SPECS["vault_search"].input_schema["properties"]["metadata_filter"]
    assert mf_schema["propertyNames"]["maxLength"] == IDENTIFIER_MAX_LEN


# ---------------------------------------------------------------------------
# Issue #36: oneOf description improvement
# ---------------------------------------------------------------------------


def test_metadata_filter_description_mentions_single_operator() -> None:
    """metadata_filter の description に「キーごとに演算子は 1 つ」の制約が明記されていること (Issue #36)."""
    from vault_search.mcp_contract import TOOL_SPECS

    desc: str = TOOL_SPECS["vault_search"].input_schema["properties"]["metadata_filter"]["description"]
    # "1 operator", "one operator", "exactly one" のいずれかを含む
    desc_lower = desc.lower()
    assert (
        "1 operator" in desc_lower
        or "one operator" in desc_lower
        or "exactly one" in desc_lower
    ), f"metadata_filter description does not mention single-operator-per-key constraint: {desc!r}"
