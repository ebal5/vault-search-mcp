# vault-search-mcp

Obsidian Vault の高速構造化検索 MCP サーバー。
SQLite FTS5 trigram + 3段キャッシュ (Tier 0-2)。

## Tech Stack

- **Python** 3.11+ / uv (自分用のため最低版を絞り、`typing.NotRequired` 等の新 API を直接使う)
- **Dependencies**: mcp, pyyaml, watchdog
- **Dev**: pytest, pytest-asyncio
- **Lint/Format**: ruff

## Commands

```bash
# Setup
uv sync

# Run (stdio mode)
uv run vault-search-mcp --vault /path/to/vault

# Test
uv run pytest

# Lint & Format
uv run ruff check --fix && uv run ruff format
```

worktree 内では sandbox が `~/.cache/uv` への書き込みを禁じるため `uv run` が
`Read-only file system` で失敗する場合がある。その場合は一度
`uv sync --all-extras` で worktree に `.venv/` を作ってから
`.venv/bin/pytest` / `.venv/bin/ruff` を直接呼ぶのが安定する (settings.json
の allow list にも登録済み)。

## Architecture

```
src/vault_search/
  server.py       — FastMCP サーバー + CLI エントリポイント
  indexer.py      — SQLite FTS5 インデクサー (VaultIndex + connection() ctxmgr)
  cache.py        — TieredCache (Tier 0/1 in-memory cache)
  watcher.py      — VaultWatcher (watchdog ベースの差分更新)
  parser.py       — Markdown パーサー (frontmatter + inline tags)
  schemas.py      — Pydantic モデル (tool output 用データ型のみ)
  mcp_contract.py — TOOL_SPECS / TOOL_ENTRIES / schema://tools payload +
                    FastMCP outputSchema 注入ハック
  filter.py       — metadata_filter 構文解析 + SQL 断片生成
  validation.py   — 入力検証 (validate_identifier / validate_value)
```

## Conventions

- ログは `logging` モジュール、出力先は stderr
- DB はデフォルトで vault_root/.vault-search.db
- 隠しフォルダ (`.`) と `_` プレフィックスフォルダは除外
- 新規 tool 追加時は `_TOOL_SPECS` (`src/vault_search/mcp_contract.py`) に `annotations`
  (`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`) を必ず設定する
- `tests/test_tool_annotations.py` の universal regression test が annotations 欠落と
  MCP spec 違反 (readOnly tool の destructive/idempotent 混在等) を検知する

## 追加方針 (別ファイル委譲)

本リポジトリは実験的な SDLC 方針を採用している。詳細は用途別に分離:

- **FastMCP の挙動上の罠** (Tool 戻り型 / outputSchema / SQL 組み立て等)
  → `.claude/rules/fastmcp-gotchas.md` — server.py / schemas.py / filter.py 改修時に必読
- **TDD ワークフロー** (Red/Green/Refactor 独立 commit、サブエージェント delegate 規約)
  → `.claude/rules/tdd-workflow.md` — 新機能追加・バグ修正時に参照
- **Issue triage / close 規約** (subagent 判定の盲信禁止、close 前個別検証)
  → `.claude/rules/issue-triage.md` — `gh issue close` する前に必読
- **レビュー品質プロセス**: `review-loop` skill を使用
  (「レビューして」「5 以上がなくなるまで」等で起動)
- **スクリプト実行**: `uv run python` または `.venv/bin/python` 直接呼びを推奨。
  一時スクリプトは `execute-script-safely` skill で事前 review してから実行
