"""``schema://tools`` payload の module-level 定数 (Issue #195).

``resources.py`` から「payload に直接乗せる module-level 定数」を分離する
軽量 module。runtime 組立 (``build_schema_payload``) と transformer
(``_serialize_error_catalog``) は ``resources.py`` 側に残し、本 module は
import 時に 1 度だけ評価される純粋な定数のみを保持する。

収めるもの:

* 静的 prose / policy (``_OVERVIEW`` / ``_VERSION_POLICY`` /
  ``_ERRORS_WIRE_FORMAT_NOTE``)
* schema 版数 (``_SCHEMA_VERSION``)
* literal dict (``_RECOMMENDED_FLOW``)
* model 由来 derived schema (``_FRONTMATTER_KEY_INFO_SCHEMA``) —
  ``FrontmatterKeyInfo.model_json_schema()`` を import 時に 1 度評価して
  以降 share する

共通契約: **payload の top-level に直接出る module-level 定数は本 module に置く**。
新しく model 由来の導出定数を追加する場合もここに置く (``resources.py`` の
``build_schema_payload`` 本文からは ``from .payload_meta import ...`` で読む)。

収めないもの:

* runtime 毎に evaluate する値 (``_serialize_error_catalog()`` の戻り)
* 組立 entry point (``build_schema_payload``)

## 分離の目的

* 「payload 組立 logic の変更」と「agent-facing 文言 / derived schema の
  微調整」の git diff を分離
* 将来の resource 追加 (``warnings://`` / ``help://tools`` 等) で
  ``resources.py`` が雪だるま式に育つのを防ぐ
* overview の 1 文字修正が serialization 実装の diff に混ざらない

## Option A (resources/ package 化) への移行 tripwire

Option B (本 module 単独分離) で十分な現段階から、以下のいずれかが起きたら
``resources/`` package への昇格を検討する:

1. 本 module の行数が 300 行を超える (現 ~220 行)
2. 2 個目の resource (``warnings://`` / ``help://tools`` / ``stats://index`` 等)
   が静的 prose を追加し、schema 用と warnings 用の定数が混載する
3. resource ごとの metadata が cross-reference を持ち始めて命名衝突する

移行時は ``resources/{schema_meta,warnings_meta,...}.py`` に分割し、
``resources/__init__.py`` で ``build_*_payload`` を re-export する構造に
する。現 import 経路 (`from .payload_meta import ...`) を
`from .resources.schema_meta import ...` 等に書き換える。

関連 watch item: #197 (``build_schema_payload`` signature 拡張方針、
resource 数が増えた時の組立関数の形状判断) は本 tripwire と同時期に
再評価するのが自然。

## 言語方針

``_OVERVIEW`` / ``_ERRORS_WIRE_FORMAT_NOTE`` / ``_RECOMMENDED_FLOW[].condition`` は
ja-JP で固定。field 名 (``step`` / ``tool`` / ``optional`` / ``condition`` 等)
のみ英語。多言語対応は i18n frontmatter (例: ``overview.en`` / ``overview.ja``
構造) が必要になった段階で defer する。

## Agent 向け prose と実装の drift guard

machine-readable なキー (``tool`` / ``error_code``) は test 層で live 参照に
よる drift guard を入れている (``test_schema_resource.py`` 参照)。一方 prose
部 (``overview`` / ``description`` / ``condition``) の内容は implementation
との drift guard を意図的に入れていない — test が fragile 化するためで、
agent 向け文言は「単一 SoT の実装記述」ではなく「自然言語ガイド」として保守
する前提。
"""

from __future__ import annotations

from typing import Any

from .schema_meta import FrontmatterKeyInfo

# ---------------------------------------------------------------------------
# schema://tools payload の top-level metadata 定数 (#38 / #179 / #195)
# ---------------------------------------------------------------------------
#
# これらは schema://tools resource payload 形式自身のメタデータであり、各 tool
# の入出力契約バージョン (tools[name].input_schema / output_schema) とは別の
# 層。payload shape を破壊する変更 (key rename / 型変更) を入れる際に手動で
# 更新する (自動 derive しない)。
#
# version format は ``<major>.<minor>`` の semver-like 文字列。bumping policy は
# payload["version_policy"] として agent に露出する (#193)。
# 2.1: errors_wire_format_note top-level key 追加 (additive, #202)、
# errors から abstract=True entry を除外 (VAULT_SEARCH_ERROR / #200 — agent
# 視点の destructive subset 変更だが ErrorCode Literal には残るため minor
# 扱い: agent が version<2.1 を cache していた場合のみ key 集合 diff が出る)。
_SCHEMA_VERSION: str = "2.1"

# payload["version"] の bumping policy。agent が cache invalidation 判断に使う。
# version 2.0 はこのポリシーを確立した版であり、同時に 1.x からの破壊的変更
# (errors の再 key 化、recommended_flow の purpose 削除) を含む。
# ポリシーは 2.0 以降の変更に適用される。
_VERSION_POLICY: str = (
    "additive changes (adding new top-level keys, adding new fields to existing "
    "objects, or adding new enum values) bump the minor version. "
    "destructive changes (renaming or removing keys, changing value types, or "
    "narrowing enum values) are breaking and bump the major version. "
    "exception: removing an entry from the 'errors' section whose underlying "
    "ErrorCode was documented as abstract (not raised directly to agents) is "
    "treated as a minor change, since such entries were not intended for agent "
    "pattern-matching; the ErrorCode itself remains in the Literal for raise "
    "fallbacks. "
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
    "bool true → 'true')。vault 本体を変更するのは vault_reindex のみで、他は全て read-only。\n\n"
    "エラー応答は FastMCP により 'Error executing tool <tool>: <message>' 形式で wrap されるため、"
    "errors セクションを使う前に top-level の errors_wire_format_note を読むこと。"
    "errors[code].example は wrap 前の raw message を示し、agent は substring matching で判定する。"
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
    "でエラー種別を判定する。error_code 属性は現状の MCP wire には含まれない。\n"
    "本セクションのキーは agent 向けに送出される具体 error_code のみで、"
    "基底例外 VaultSearchError (ErrorCode Literal には含まれるが abstract) は"
    "本 payload に含まれない。"
)


# Pydantic v2 は同一モデルに対する model_json_schema() を内部キャッシュするが、
# ここでも import 時に 1 度だけ評価して以降 dict instance を共有する。
# agent が value_type の許容値 enum を先に読んでから frontmatter_keys を解釈する
# ため、payload 上も frontmatter_keys より前に置かれる。
_FRONTMATTER_KEY_INFO_SCHEMA: dict[str, Any] = FrontmatterKeyInfo.model_json_schema()
