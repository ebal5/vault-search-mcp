# vault-search-mcp

Obsidian Vault の高速構造化検索 MCP サーバー。
SQLite FTS5 trigram + 3段キャッシュ (Tier 0-2)。

## Tech Stack

- **Python** 3.10+ / uv
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

## Architecture

```
src/vault_search/
  server.py   — FastMCP サーバー + CLI エントリポイント
  indexer.py  — SQLite FTS5 インデクサー + TieredCache + VaultWatcher
  parser.py   — Markdown パーサー (frontmatter + inline tags)
```

## Conventions

- ログは `logging` モジュール、出力先は stderr
- DB はデフォルトで vault_root/.vault-search.db
- 隠しフォルダ (`.`) と `_` プレフィックスフォルダは除外
