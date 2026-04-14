# FastMCP の落とし穴

本プロジェクトは FastMCP の挙動上の罠に繰り返し遭遇した。以降の改修時は
以下を必ず踏まえる。

## Tool 戻り型

- **Union 型 (`Model | dict[str, Any]`) を使うと `wrap_output=True` が発動**し、
  MCP structuredContent が `{"result": ...}` で 1 段ラップされる
- **単一 `dict[str, Any]` 推奨** — wrap なしで flat JSON が返る
- Pydantic モデル戻りは `model_validate + model_dump` (exclude_unset なし) で
  再シリアライズされるため、`model_construct` による部分モデルが全フィールド
  復元される → fields 系のフィルタは dict を手動で返す以外に方法なし
- List 戻り (`list[Model]`) も FastMCP に wrap される → envelope dict
  (`{"tags": [...]}`, `{"folders": [...]}`, `{"notes": [...]}`) に統一

## outputSchema の手動注入

- `@mcp.tool()` に `output_schema` 引数がないため、dict 戻り型は generic な
  `{"additionalProperties": true}` になる
- rich schema は起動時に `_inject_rich_output_schemas()` で
  `_tool_manager.tools[name].fn_metadata.output_schema` を書き換え
  (内部 API 依存、FastMCP upgrade 時注意、TODO コメント参照)
- MCP lowlevel は structuredContent に対し
  `jsonschema.validate(instance, outputSchema)` を強制する
  → fields subset 対応は `anyOf: [full, subset]` 形にして両立

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

## 将来の解消候補

- FastMCP 上流で `@tool(output_schema=...)` 公式対応が入れば
  `_inject_rich_output_schemas()` のハックは撤去可能
- `exclude_unset=True` 相当を FastMCP が標準サポートすれば、fields 機能の
  dict 手動返却も Pydantic モデル戻りに戻せる
