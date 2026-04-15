"""MCP tool annotations (readOnlyHint / destructiveHint / idempotentHint /
openWorldHint) が全ツールに MCP spec 準拠で付与されていることを検証する regression テスト.

Issue #22 (B7) + review round 1 (Reviewer A/B/C/D) follow-up:

* 厳格 MCP クライアント (Claude Desktop の auto-approve 設定など) が annotations を
  読んで承認判定するため、各ツールに適切な hint を付与する。
* spec 準拠:
  - ``readOnlyHint=True`` なツールでは ``idempotentHint`` / ``destructiveHint`` は
    「意味を持たない」 (MCP spec の ToolAnnotations 定義) ので ``None`` 指定
    (FastMCP は ``None`` のフィールドを wire 上で落とす)。
  - ``destructiveHint`` は user-facing データの不可逆損失を指す。派生キャッシュ
    (``.vault-search.db``) の再構築は user-facing vault を touch しないため、
    ``vault_reindex`` でも ``False`` とする (auto-approve UX への悪影響を避ける)。
  - ``openWorldHint=False`` は全ツールで統一 (ローカル vault のみを扱う)。
* regression guard: 新規 tool 追加時に annotations を付け忘れた場合、universal
  test が検知する (hardcode allowlist に頼らない)。
* canonical source 統一: schema://tools リソースも annotations を露出する
  (MCP tools/list と schema://tools の metadata drift を防ぐ)。
"""

from __future__ import annotations

import asyncio

import pytest

from vault_search import server as server_mod
from vault_search.indexer import VaultIndex
from vault_search.mcp_contract import build_schema_payload

# 期待 annotations マトリクス (MCP spec 準拠).
# ``None`` は「MCP spec で意味を持たないため意図的に未設定」を示す。
_EXPECTED_ANNOTATIONS: dict[str, dict[str, bool | None]] = {
    "vault_search": {
        "readOnlyHint": True,
        "destructiveHint": None,
        "idempotentHint": None,
        "openWorldHint": False,
    },
    "vault_get_note": {
        "readOnlyHint": True,
        "destructiveHint": None,
        "idempotentHint": None,
        "openWorldHint": False,
    },
    "vault_recent": {
        "readOnlyHint": True,
        "destructiveHint": None,
        "idempotentHint": None,
        "openWorldHint": False,
    },
    "vault_tags": {
        "readOnlyHint": True,
        "destructiveHint": None,
        "idempotentHint": None,
        "openWorldHint": False,
    },
    "vault_folders": {
        "readOnlyHint": True,
        "destructiveHint": None,
        "idempotentHint": None,
        "openWorldHint": False,
    },
    "vault_stats": {
        "readOnlyHint": True,
        "destructiveHint": None,
        "idempotentHint": None,
        "openWorldHint": False,
    },
    "vault_reindex": {
        "readOnlyHint": False,
        # 派生 DB のみ書き換え、user-facing vault (.md) は touch しない。
        # MCP spec の "destructive" は user-facing の不可逆損失を指すため False。
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


@pytest.mark.parametrize(("tool_name", "expected"), list(_EXPECTED_ANNOTATIONS.items()))
def test_tool_annotations_match_expected(
    tool_name: str,
    expected: dict[str, bool | None],
) -> None:
    """各 tool の MCP annotations が期待値と一致する (spec 準拠)."""
    tools = asyncio.run(server_mod.mcp.list_tools())
    tool = next((t for t in tools if t.name == tool_name), None)
    assert tool is not None, f"tool not registered: {tool_name}"

    annotations = tool.annotations
    assert annotations is not None, f"{tool_name} has no annotations"

    for key, expected_value in expected.items():
        actual = getattr(annotations, key, None)
        assert actual == expected_value, (
            f"{tool_name}.{key}: expected {expected_value!r}, got {actual!r}"
        )


def test_every_registered_tool_has_annotations() -> None:
    """MCP list_tools で返る全 tool に annotations が付与されている.

    新規 tool 追加時に annotations を付け忘れても hardcode allowlist は
    検知できないため、``list_tools()`` の出力を基準に universal guard を張る。
    """
    tools = asyncio.run(server_mod.mcp.list_tools())
    missing = [t.name for t in tools if t.annotations is None]
    assert missing == [], f"tools without annotations: {missing}"

    # _EXPECTED_ANNOTATIONS 側と tools/list の tool 集合が一致することも検査。
    # 新規 tool 追加時は両方を更新しないとここで失敗する。
    registered = {t.name for t in tools}
    expected_covered = set(_EXPECTED_ANNOTATIONS)
    assert registered == expected_covered, (
        f"symmetric difference between registered tools and expected matrix: "
        f"registered-only={registered - expected_covered}, "
        f"expected-only={expected_covered - registered}"
    )


def test_only_vault_reindex_is_writer() -> None:
    """書き込み可能 tool は vault_reindex のみ (アーキテクチャ不変条件).

    将来 write-tool を追加した際にこのテストが失敗することで「意図的な設計変更」
    として明示的にレビュー対象になる。silent に writer が増えないよう固定する。
    """
    tools = asyncio.run(server_mod.mcp.list_tools())
    writers = sorted(
        t.name for t in tools if t.annotations is not None and t.annotations.readOnlyHint is False
    )
    assert writers == ["vault_reindex"], (
        f"unexpected writer(s) detected — update this invariant intentionally: {writers}"
    )


def test_readonly_tools_do_not_declare_destructive_or_idempotent() -> None:
    """``readOnlyHint=True`` ツールは destructive/idempotent を宣言しない (MCP spec 準拠).

    MCP spec の ``destructiveHint`` / ``idempotentHint`` は
    「``readOnlyHint == false`` のときのみ意味を持つ」と定義されているため、
    readOnly tool では ``None`` のまま残す。spec 違反を防ぐ invariant。
    """
    tools = asyncio.run(server_mod.mcp.list_tools())
    offenders: list[str] = []
    for tool in tools:
        if tool.annotations is None or tool.annotations.readOnlyHint is not True:
            continue
        if tool.annotations.destructiveHint is not None:
            offenders.append(f"{tool.name}.destructiveHint={tool.annotations.destructiveHint}")
        if tool.annotations.idempotentHint is not None:
            offenders.append(f"{tool.name}.idempotentHint={tool.annotations.idempotentHint}")
    assert offenders == [], (
        f"readOnly tools must leave destructive/idempotent unset (MCP spec): {offenders}"
    )


def test_all_tools_closed_world() -> None:
    """全 tool は ``openWorldHint=False`` (ローカル vault のみを扱う設計前提)."""
    tools = asyncio.run(server_mod.mcp.list_tools())
    violations = [
        t.name for t in tools if t.annotations is None or t.annotations.openWorldHint is not False
    ]
    assert violations == [], f"tools violating closed-world invariant: {violations}"


def test_schema_tools_resource_exposes_annotations(vault_index: VaultIndex) -> None:
    """``schema://tools`` リソースも各 tool の annotations を公開する.

    fastmcp-gotchas.md の canonical-unification 原則: MCP ``tools/list`` と
    ``schema://tools`` は同一メタデータを露出すべき。annotations を MCP 経路
    にしか出していないと、``schema://tools`` だけを読む構造化 agent が tool
    の副作用性を判定できず drift する。
    """
    payload = build_schema_payload(vault_index.list_frontmatter_keys())
    tools_entries = payload["tools"]
    assert set(tools_entries.keys()) == set(_EXPECTED_ANNOTATIONS), (
        f"schema://tools tool set mismatch: {set(tools_entries.keys())}"
    )
    for tool_name, expected in _EXPECTED_ANNOTATIONS.items():
        entry = tools_entries[tool_name]
        assert "annotations" in entry, f"{tool_name}: schema://tools missing 'annotations' key"
        annotations = entry["annotations"]
        assert isinstance(annotations, dict), (
            f"{tool_name}: annotations must be dict, got {type(annotations).__name__}"
        )
        for key, expected_value in expected.items():
            if expected_value is None:
                # None 相当は dict から落としてよい (MCP wire 挙動と揃える)
                assert annotations.get(key) is None, (
                    f"{tool_name}.{key}: expected unset/None, got {annotations.get(key)!r}"
                )
            else:
                assert annotations.get(key) == expected_value, (
                    f"{tool_name}.{key}: expected {expected_value!r}, got {annotations.get(key)!r}"
                )
