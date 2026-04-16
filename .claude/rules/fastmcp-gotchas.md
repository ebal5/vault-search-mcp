# FastMCP の落とし穴

本プロジェクトは FastMCP の挙動上の罠に繰り返し遭遇した。以降の改修時は
以下を必ず踏まえる。

## Tool 戻り型

- **Union 型 (`Model | dict[str, Any]`) を使うと `wrap_output=True` が発動**し、
  MCP structuredContent が `{"result": ...}` で 1 段ラップされる
- **単一 `dict[str, Any]` 推奨** — wrap なしで flat JSON が返る
- Pydantic モデル戻りは `model_validate + model_dump` (exclude_unset なし) で
  再シリアライズされるため、部分モデルでの出力フィルタは不可
  (runtime で全フィールド復元される)
- List 戻り (`list[Model]`) も FastMCP に wrap される → envelope dict
  (`{"tags": [...]}`, `{"folders": [...]}`, `{"notes": [...]}`) に統一

## outputSchema の手動注入

- `@mcp.tool()` に `output_schema` 引数がないため、dict 戻り型は generic な
  `{"additionalProperties": true}` になる
- rich schema は起動時に `mcp_contract.inject_rich_output_schemas(mcp)` で
  `_tool_manager._tools[name].fn_metadata.output_schema` を書き換え
  (内部 API 依存、FastMCP upgrade 時注意、TODO コメントは mcp_contract.py 内に集約)
- MCP lowlevel は structuredContent に対し
  `jsonschema.validate(instance, outputSchema)` を強制する
  → 注入する schema は実レスポンスと厳密一致させる (`extra="forbid"` の
  Pydantic モデル由来なら OK)

## テスト経路

- `FastMCP.call_tool()` は `(content_blocks, structured)` tuple を返す
- テストで call_tool のみ通すと MCP lowlevel の jsonschema.validate をバイパスする
- `jsonschema.validate(structured, tool.outputSchema)` を明示的にかけることで
  lowlevel 経路の regression を検知する (`tests/test_mcp_wrapping.py` 参照)

## metadata_filter の SQL 組み立て

- `filter.py:build_sql_fragment` が `cond.key` を `f"$.{key}"` に展開する
- **validate_identifier でキー名が検証済みであること**が SQL injection 防御の
  前提。検証されていないキーを絶対に SQL 文字列に埋め込まない
- `eq` / `in` / `ne` の全演算子で scalar / array 対応の非対称性に注意
  (`ne` の配列対応は `NOT EXISTS(json_each...)` が必須)

## Tool error — 構造化属性の wire 消失

- FastMCP `Tool.run()` が例外をキャッチして
  `ToolError(f"Error executing tool {name}: {e}") from e` で再 raise する
- MCP lowlevel は `_make_error_result(str(e))` で `TextContent` のみの error
  response を生成する
- 結果: `ValidationError` が持つ `error_code` / `did_you_mean` / `allowed` /
  `unknown_keys` / `hint` 属性はエージェントに届かない — `str(err)` 経由の
  平文メッセージのみが届く
- 影響: PR #135 (#123) で追加した `unknown_keys` batch 情報は MCP 実運用では
  不可視、`error_code` での programmatic 分岐も不可
- **本プロジェクトの現状ポリシー: 受容** (workaround 非採用)
  - `ValidationError.__str__` を JSON-like にする案は message 冗長化の副作用
    があり採用見送り
  - 長期的には上流 MCP SDK の structured error payload 対応待ち
- 撤去候補: MCP 2.0 / FastMCP 上流で `ToolError` が structured payload を
  サポートした時点で本 Gotcha は解消

## Tool annotations

- **付与経路**: `mcp_contract.py` の `_TOOL_SPECS[name].annotations` に
  `ToolAnnotations` を設定し、`server.py` の `@mcp.tool(annotations=...)` で
  FastMCP へ wire する
- **公開経路**: MCP `tools/list` と `schema://tools` リソースの両方に出る。
  両経路の drift を防ぐため `_build_tool_entry` が `entry["annotations"]` を
  `exclude_none=True` でシリアライズして `TOOL_ENTRIES` に含める
- **MCP spec 準拠点**: `readOnlyHint=true` のツールでは
  `destructiveHint` / `idempotentHint` を `None` (未設定) にする。
  spec は "meaningful only when readOnlyHint=false" と定義しており、
  FastMCP は `None` フィールドを wire 上から落とす
- **`vault_reindex` の `destructiveHint=false`**: user-facing vault (`.md`)
  を touch せず派生 DB のみを再構築するため。auto-approve クライアントへの
  過剰な警告を避ける意図的設定
- **regression guard**: `tests/test_tool_annotations.py` が annotations
  欠落・MCP spec 違反・wire serialize regression を検知する universal test。
  新規 tool を追加したらこのテストが失敗するため、annotations の付け忘れを
  構造的に防止できる
- **FastMCP upgrade 時のリスク**: `@mcp.tool(annotations=...)` の
  `annotations` 引数が silent に drop または rename されても現状テストで
  検知できる。ただし FastMCP 内部の `ToolAnnotations` デフォルト値が変わった
  場合 (例: `None` → `False`) は
  `test_tool_annotations_exclude_none_wire_omits_null_hints` が catch する

## 将来の解消候補

- FastMCP 上流で `@tool(output_schema=...)` 公式対応が入れば
  `mcp_contract.inject_rich_output_schemas()` のハックは撤去可能
- MCP lowlevel の `ToolError` wrap が structured payload を通せるようになれば、
  `ValidationError` 属性 (`error_code` 等) が agent に届くようになる
