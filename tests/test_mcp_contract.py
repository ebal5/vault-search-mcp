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
