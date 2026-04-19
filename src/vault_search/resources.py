"""MCP リソース payload の runtime 組み立て.

`mcp_contract.py` は tool 契約 (schema 生成) に専念し、実データを wire 形式へ
serialize する責務は本 module が担う (Issue #184)。`schema://tools` resource
handler は本 module の関数を呼び出すだけ。

Top-level metadata (version / overview / recommended_flow / errors /
frontmatter_key_info_schema) も本 module の module-level 定数として集約する
(Issue #38 / #179)。各定数は import 時に 1 度だけ評価され、以降は毎回同じ
オブジェクトが payload に挿入される。

## Read-only 契約

``build_schema_payload`` が返す dict は ``_RECOMMENDED_FLOW`` / ``_ERRORS`` /
``TOOL_ENTRIES`` 等の module-level 定数への直接参照を含む。呼出側は返り値を
**read-only として扱うこと** — mutate すると以降の呼出および他セッションに
波及する。MCP resource 経路は即座に JSON serialize するためこの制約で実害は
出ないが、future 拡張 (test や非 MCP 呼出) での罠を避けるため明記する。

## 言語方針

``_OVERVIEW`` / ``_RECOMMENDED_FLOW[].purpose`` / ``_ERRORS[].description`` は
ja-JP で固定。field 名 (``version`` / ``step`` / ``tool`` / ``purpose`` /
``error_code`` 等) のみ英語。多言語対応は i18n frontmatter (例:
``overview.en`` / ``overview.ja`` 構造) が必要になった段階で defer する。

## Agent 向け prose と実装の drift guard

machine-readable なキー (``tool`` / ``error_code``) は test 層で live 参照に
よる drift guard を入れている (``test_schema_resource.py`` 参照)。一方 prose
部 (``overview`` / ``purpose`` / ``description``) の内容は implementation
との drift guard を意図的に入れていない — test が fragile 化するためで、
agent 向け文言は「単一 SoT の実装記述」ではなく「自然言語ガイド」として保守
する前提。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .exceptions import NoteNotFoundError
from .mcp_contract import TOOL_ENTRIES
from .schema_meta import FrontmatterKeyInfo
from .validation import ValidationError

__all__ = ["build_schema_payload"]


# ---------------------------------------------------------------------------
# schema://tools payload の top-level metadata 定数 (#38 / #179)
# ---------------------------------------------------------------------------
#
# これらは schema://tools resource payload 形式自身のメタデータであり、各 tool
# の入出力契約バージョン (tools[name].input_schema / output_schema) とは別の
# 層。payload shape を破壊する変更 (key rename / 型変更) を入れる際に手動で
# 更新する (自動 derive しない)。
_SCHEMA_VERSION: str = "1.0"

_OVERVIEW: str = (
    "vault-search-mcp は Obsidian Vault を構造化された知識ベースとして公開する MCP サーバー。"
    "SQLite FTS5 trigram インデックスで日英両対応の全文検索を提供し、note 単位の frontmatter を"
    "機械可読な値として metadata_filter 経由で絞り込める。\n\n"
    "エージェントはまず本 schema://tools resource を読み、利用可能な tool 一覧、"
    "frontmatter_keys の型・値例、代表エラーの構造を把握してから recommended_flow の順で "
    "tool を呼び出すことが推奨される。全スカラー frontmatter 値は index 時に文字列へ"
    "正規化される (例: int 5 → '5'、date 2024-01-15 → '2024-01-15'、"
    "bool true → 'true')。vault 本体を変更するのは vault_reindex のみで、他は全て read-only。"
)

# Tool 名は TOOL_SPECS 経由で tests/test_schema_resource.py の drift guard が照合する。
# schema://tools resource 自身は step 0 (overview の冒頭) で触れる想定で本 flow には含めない。
_RECOMMENDED_FLOW: list[dict[str, Any]] = [
    {
        "step": 1,
        "tool": "vault_folders",
        "purpose": "フォルダ構造を列挙して後続検索の scope を決める",
    },
    {
        "step": 2,
        "tool": "vault_tags",
        "purpose": "タグ一覧を取得して metadata_filter の候補を把握する",
    },
    {
        "step": 3,
        "tool": "vault_search",
        "purpose": "query と tags / folder / metadata_filter を組み合わせて全文検索する",
    },
    {
        "step": 4,
        "tool": "vault_get_note",
        "purpose": "検索で見つけた path を指定して note 本文を取得する",
    },
    {
        "step": 5,
        "tool": "vault_recent",
        "purpose": "最近編集された note を取得したい場合の補助",
    },
    {
        "step": 6,
        "tool": "vault_stats",
        "purpose": "index の健全性と note 数を確認する",
    },
    {
        "step": 7,
        "tool": "vault_reindex",
        "purpose": "watcher 外の変更や破損があった場合のみ index を再構築する (通常不要)",
    },
]

# error_code は live class 属性を参照して drift を防ぐ
# (tests/test_schema_resource.py::test_errors_error_code_matches_live_exception_class)。
#
# MCP wire format 注記: FastMCP は例外を ToolError でラップするため、agent が
# 実際に受け取る文字列は 'Error executing tool <tool_name>: <raw_message>' 形式。
# `example` 値は raw_message 部分のみを示す (.claude/rules/fastmcp-gotchas.md
# の「Tool error — 構造化属性の wire 消失」節参照)。`error_code` 属性は
# 現状の MCP wire には含まれないため、agent は error_code ベースの programmatic
# 分岐ではなく message 文字列を見ることになる。
_ERRORS: dict[str, dict[str, str]] = {
    "NoteNotFoundError": {
        "error_code": NoteNotFoundError.error_code,
        "description": (
            "指定された path の note が index に存在しない。"
            "vault_search や vault_folders で path を先に確認してから再試行する。"
        ),
        "example": "Note not found: Projects/foo.md",
    },
    "ValidationError": {
        "error_code": ValidationError.error_code,
        "description": (
            "エージェント入力の検証失敗 (識別子不正 / 未知の frontmatter key / "
            "ページング範囲外 など)。hint や did_you_mean で自己修正ヒントを付けて返す。"
        ),
        "example": "Unknown frontmatter key 'statu'; did you mean: status?",
    },
}

# Pydantic v2 は同一モデルに対する model_json_schema() を内部キャッシュするが、
# ここでも import 時に 1 度だけ評価して以降 dict instance を共有する。
_FRONTMATTER_KEY_INFO_SCHEMA: dict[str, Any] = FrontmatterKeyInfo.model_json_schema()


def build_schema_payload(
    frontmatter_keys: Iterable[FrontmatterKeyInfo],
) -> dict[str, Any]:
    """schema://tools resource payload を組み立てる.

    呼び出し側は ``VaultIndex.list_frontmatter_keys()`` 相当の反復可能オブジェクトを
    そのまま渡す。Pydantic モデルを wire 形式の dict へ serialize するのは
    resource layer (本 module) の責務。
    """
    return {
        "version": _SCHEMA_VERSION,
        "overview": _OVERVIEW,
        "recommended_flow": _RECOMMENDED_FLOW,
        "errors": _ERRORS,
        "tools": TOOL_ENTRIES,
        # frontmatter_key_info_schema を frontmatter_keys より前に置くことで
        # agent が value_type の許容値 enum を先に読んでから実データを解釈できる。
        "frontmatter_key_info_schema": _FRONTMATTER_KEY_INFO_SCHEMA,
        "frontmatter_keys": [item.model_dump(mode="json") for item in frontmatter_keys],
    }
