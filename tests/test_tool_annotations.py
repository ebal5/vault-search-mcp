"""MCP tool annotations (readOnlyHint / destructiveHint / idempotentHint /
openWorldHint) が全ツールに付与されていることを検証する regression テスト.

Issue #22 (B7): 厳格な MCP クライアント (Claude Desktop の auto-approve 設定など)
は annotations を見て承認判定する。未付与だと全 tool が「副作用ありかも」扱いに
なり UX が劣化するため、各ツールに適切な hint を付与する。

annotation は「ヒント」であり保証ではないが、本プロジェクトの全ツールは以下:

- 読み取り系 (vault_search / vault_get_note / vault_recent / vault_tags /
  vault_folders / vault_stats): readOnly=true, destructive=false,
  idempotent=true, openWorld=false (ローカル vault のみ)
- 書き込み系 (vault_reindex): readOnly=false, destructive=true (全件リビルド時に
  既存 DB を置換)、idempotent=true (同一入力で同一状態に収束)、openWorld=false
"""

from __future__ import annotations

import asyncio

import pytest

from vault_search import server as server_mod
from vault_search.indexer import VaultIndex

# 期待 annotations マトリクス (issue #22 の表を全 tool に拡張).
# 値は bool リテラル: None 扱いにせず必ず明示する。
_EXPECTED_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "vault_search": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "vault_get_note": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "vault_recent": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "vault_tags": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "vault_folders": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "vault_stats": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "vault_reindex": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


@pytest.fixture(autouse=True)
def _inject_index(vault_index: VaultIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "_index", vault_index)


@pytest.mark.parametrize(("tool_name", "expected"), list(_EXPECTED_ANNOTATIONS.items()))
def test_tool_annotations_match_expected(
    tool_name: str,
    expected: dict[str, bool],
    vault_index: VaultIndex,
) -> None:
    """各 tool の MCP annotations が期待値と一致する.

    MCP tools/list 経路 (``list_tools()``) で返る ``Tool.annotations`` を直接検証。
    """
    tools = asyncio.run(server_mod.mcp.list_tools())
    tool = next((t for t in tools if t.name == tool_name), None)
    assert tool is not None, f"tool not registered: {tool_name}"

    annotations = tool.annotations
    assert annotations is not None, f"{tool_name} has no annotations"

    for key, expected_value in expected.items():
        actual = getattr(annotations, key, None)
        assert actual == expected_value, (
            f"{tool_name}.{key}: expected {expected_value}, got {actual}"
        )
