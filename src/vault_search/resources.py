"""MCP リソース payload の runtime 組み立て.

`mcp_contract.py` は tool 契約 (schema 生成) に専念し、実データを wire 形式へ
serialize する責務は本 module が担う (Issue #184)。`schema://tools` resource
handler は本 module の関数を呼び出すだけ。

Top-level metadata (version / overview / recommended_flow / errors /
frontmatter_key_info_schema) も本 module の module-level 定数として集約する
(Issue #38 / #179)。各定数は import 時に 1 度だけ評価され、以降は毎回同じ
オブジェクトが payload に挿入される。

## Read-only 契約

``build_schema_payload`` が返す dict は ``_RECOMMENDED_FLOW`` / ``TOOL_ENTRIES``
等の module-level 定数への直接参照を含む (``errors`` は
``_serialize_error_catalog()`` が毎回新 dict を作るため share されない)。
呼出側は返り値を **read-only として扱うこと** — mutate すると以降の呼出
および他セッションに波及する。MCP resource 経路は即座に JSON serialize する
ためこの制約で実害は出ないが、future 拡張 (test や非 MCP 呼出) での罠を
避けるため明記する。

## 言語方針

``_OVERVIEW`` / ``_ERRORS_WIRE_FORMAT_NOTE`` / ``ERROR_CATALOG[].description`` /
``_RECOMMENDED_FLOW[].condition`` は ja-JP で固定 (後者 2 つは ``exceptions.py``
側で定義)。field 名 (``version`` / ``step`` / ``tool`` / ``optional`` /
``condition`` / ``error_code`` 等) のみ英語。多言語対応は i18n frontmatter (例:
``overview.en`` / ``overview.ja`` 構造) が必要になった段階で defer する。

## Agent 向け prose と実装の drift guard

machine-readable なキー (``tool`` / ``error_code``) は test 層で live 参照に
よる drift guard を入れている (``test_schema_resource.py`` 参照)。一方 prose
部 (``overview`` / ``description`` / ``condition``) の内容は implementation
との drift guard を意図的に入れていない — test が fragile 化するためで、
agent 向け文言は「単一 SoT の実装記述」ではなく「自然言語ガイド」として保守
する前提。

``_RECOMMENDED_FLOW`` は呼出順序 + ``optional`` / ``condition`` メタデータの契約
(#192) に限定し、tool 個別の用途説明 (旧 ``purpose``) は持たない (#196 Option A)。
tool 個別の説明は ``tools[name].description`` を単一 SoT として参照する。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .exceptions import ERROR_CATALOG
from .mcp_contract import TOOL_ENTRIES
from .schema_meta import FrontmatterKeyInfo

__all__ = ["build_schema_payload"]


# ---------------------------------------------------------------------------
# schema://tools payload の top-level metadata 定数 (#38 / #179)
# ---------------------------------------------------------------------------
#
# これらは schema://tools resource payload 形式自身のメタデータであり、各 tool
# の入出力契約バージョン (tools[name].input_schema / output_schema) とは別の
# 層。payload shape を破壊する変更 (key rename / 型変更) を入れる際に手動で
# 更新する (自動 derive しない)。
#
# version format は ``<major>.<minor>`` の semver-like 文字列。bumping policy は
# payload["version_policy"] として agent に露出する (#193)。
_SCHEMA_VERSION: str = "2.0"

# payload["version"] の bumping policy。agent が cache invalidation 判断に使う。
# version 2.0 はこのポリシーを確立した版であり、同時に 1.x からの破壊的変更
# (errors の再 key 化、recommended_flow の purpose 削除) を含む。
# ポリシーは 2.0 以降の変更に適用される。
_VERSION_POLICY: str = (
    "additive changes (adding new top-level keys, adding new fields to existing "
    "objects, or adding new enum values) bump the minor version. "
    "destructive changes (renaming or removing keys, changing value types, or "
    "narrowing enum values) are breaking and bump the major version. "
    "agents should invalidate cached schema payloads on any major version change "
    "and re-read the payload; minor version changes are safe to ignore if the "
    "agent only consumes known keys. "
    "this policy applies to changes made from version 2.0 onward; version 2.0 "
    "itself established this policy alongside breaking structural changes from 1.x."
)

_OVERVIEW: str = (
    "vault-search-mcp は Obsidian Vault を構造化された知識ベースとして公開する MCP サーバー。"
    "SQLite FTS5 trigram インデックスで日英両対応の全文検索を提供し、note 単位の frontmatter を"
    "機械可読な値として metadata_filter 経由で絞り込める。\n\n"
    "エージェントはまず本 schema://tools resource を読み、利用可能な tool 一覧、"
    "frontmatter_keys の型・値例、代表エラーの構造を把握してから recommended_flow を参考に "
    "tool を呼び出すことが推奨される。recommended_flow は全ステップが optional であり、"
    "各 step の ``condition`` フィールドでそのステップを呼ぶ判断基準を機械可読に示す "
    "(step 3 vault_search が「ほとんどのタスクの起点」)。各 tool の詳細挙動・引数・戻り値は "
    "tools[name].description / input_schema / output_schema を参照する。\n\n"
    "全スカラー frontmatter 値は index 時に文字列へ"
    "正規化される (例: int 5 → '5'、date 2024-01-15 → '2024-01-15'、"
    "bool true → 'true')。vault 本体を変更するのは vault_reindex のみで、他は全て read-only。"
)

# Tool 名は server.mcp.list_tools() 経由で tests/test_schema_resource.py の drift guard が
# 照合する (#194)。schema://tools resource 自身は step 0 (overview の冒頭) で触れる想定で
# 本 flow には含めない。
#
# 各 step は ``optional: bool`` を持つ。optional=True の step は ``condition``
# フィールドで発動条件を人間可読かつ machine-parseable な短文で示す (#192)。
# 詳細な purpose 説明は ``tools[name].description`` に寄せ、本 flow には含めない
# (#196 Option A: drift guard 面積を排除)。
_RECOMMENDED_FLOW: list[dict[str, Any]] = [
    {
        "step": 1,
        "tool": "vault_folders",
        "optional": True,
        "condition": "フォルダ構造が未知で、後続検索の scope を事前に絞りたい場合",
    },
    {
        "step": 2,
        "tool": "vault_tags",
        "optional": True,
        "condition": "利用可能なタグ一覧が未知で、metadata_filter の候補を把握したい場合",
    },
    {
        "step": 3,
        "tool": "vault_search",
        "optional": True,
        "condition": (
            "ほとんどのタスクの起点。"
            "テキスト / タグ / フォルダ / metadata_filter 条件でノートを絞り込む場合"
        ),
    },
    {
        "step": 4,
        "tool": "vault_get_note",
        "optional": True,
        "condition": (
            "特定ノートの全文・frontmatter を取得する場合 "
            "(vault_search の path または既知の path を使用)"
        ),
    },
    {
        "step": 5,
        "tool": "vault_recent",
        "optional": True,
        "condition": "最近編集された note を起点に探索したい場合 (query なしの補助的な起点)",
    },
    {
        "step": 6,
        "tool": "vault_stats",
        "optional": True,
        "condition": "index の健全性 / note 総数を確認したい場合 (診断用途)",
    },
    {
        "step": 7,
        "tool": "vault_reindex",
        "optional": True,
        "condition": "watcher 外の変更や index 破損が疑われる場合のみ (通常は不要)",
    },
]

# errors payload の共通 wrap note (#202).
#
# FastMCP は ``Tool.run()`` 内で例外を ``ToolError(f"Error executing tool {name}: {e}")``
# として wrap するため、agent が実際に受け取る message は全 error 共通で
# ``"Error executing tool <tool>: <raw>"`` 形式。以前は ValidationError の
# description にだけ wrap note が書かれており entry 間で情報量が不均一だった
# (#202)。top-level に吊り上げて共通化することで、各 entry の description は
# error 固有の意味 (何が起きたか / どう直すか) に集中できる。
#
# agent は ``errors[code].example`` の raw message を substring として message
# 全体にマッチする。``error_code`` 属性は現状の MCP wire には含まれないため
# (.claude/rules/fastmcp-gotchas.md 「Tool error — 構造化属性の wire 消失」節
# 参照)、programmatic 分岐は substring matching で行う。
_ERRORS_WIRE_FORMAT_NOTE: str = (
    "FastMCP は全ての例外を 'Error executing tool <tool>: <message>' 形式の"
    "プレーンテキストに wrap するため、agent が受け取る message 文字列は常に"
    "この prefix を持つ。各 entry の example は wrap 前の raw message を示すので、"
    "agent は substring matching (e.g. 'Unknown frontmatter key' が message に含まれる) "
    "でエラー種別を判定する。error_code 属性は現状の MCP wire には含まれない。"
)


def _serialize_error_catalog() -> dict[str, dict[str, str]]:
    """``ERROR_CATALOG`` を agent-facing wire 形式に変換する (#199 / #200 / #201).

    * ``exception_class`` は live class 参照なので ``__name__`` に展開する
      (raised_by)
    * ``abstract=True`` の entry は除外 — ``VaultSearchError`` のような基底例外
      が agent の pattern-match 対象に混ざらないようにする (#200)
    * 戻り dict の key は ``ErrorCode`` Literal 値と一致する (string literal に
      統一、live class attr は参照しない #199)
    """
    return {
        code: {
            "raised_by": info["exception_class"].__name__,
            "description": info["description"],
            "example": info["example"],
        }
        for code, info in ERROR_CATALOG.items()
        if not info.get("abstract", False)
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
        "version_policy": _VERSION_POLICY,
        "overview": _OVERVIEW,
        "recommended_flow": _RECOMMENDED_FLOW,
        "errors_wire_format_note": _ERRORS_WIRE_FORMAT_NOTE,
        "errors": _serialize_error_catalog(),
        "tools": TOOL_ENTRIES,
        # frontmatter_key_info_schema を frontmatter_keys より前に置くことで
        # agent が value_type の許容値 enum を先に読んでから実データを解釈できる。
        "frontmatter_key_info_schema": _FRONTMATTER_KEY_INFO_SCHEMA,
        "frontmatter_keys": [item.model_dump(mode="json") for item in frontmatter_keys],
    }
