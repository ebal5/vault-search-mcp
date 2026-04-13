# vault-search-mcp

Obsidian Vault の高速構造化検索を MCP (Model Context Protocol) で提供するサーバー。

## 特徴

- **FTS5 trigram** — 日英混在テキストの全文検索（外部トークナイザ不要）
- **3段キャッシュ** — ByteRover 式プログレッシブ検索 (Tier 0: ~0ms, Tier 1: ~1ms, Tier 2: ~10ms)
- **ファイル監視** — watchdog による差分インデックス更新
- **frontmatter 対応** — タグ・日付・フォルダでの構造化フィルタリング
- **ヘッドレス運用** — Obsidian アプリ不要、ファイルシステム直アクセス

## MCP ツール一覧

| ツール | 説明 |
|---|---|
| `vault_search` | 全文検索（タグ・フォルダフィルタ付き） |
| `vault_get_note` | 単一ノートの全文取得 |
| `vault_recent` | 最近更新されたノート一覧 |
| `vault_tags` | 全タグと出現回数 |
| `vault_folders` | フォルダ構造とノート数 |
| `vault_reindex` | インデックス再構築 |
| `vault_stats` | インデックス統計情報 |

## セットアップ（uv）

```bash
cd vault-search-mcp

# 依存解決 + venv 作成 + ロックファイル生成
uv sync

# 起動（stdio モード）
uv run vault-search-mcp --vault /path/to/vault

# DB パスのカスタマイズ
uv run vault-search-mcp --vault /path/to/vault --db /data/vault-search.db

# ファイル監視なし（起動時インデックスのみ）
uv run vault-search-mcp --vault /path/to/vault --no-watch

# python -m でも起動可
uv run python -m vault_search --vault /path/to/vault
```

環境変数でも指定可能:

```bash
VAULT_ROOT=/path/to/vault VAULT_SEARCH_DB=/data/vault-search.db uv run vault-search-mcp
```

## Hermes Agent 統合

### config.yaml（uv 経由）

```yaml
mcp_servers:
  vault-search:
    command: "uv"
    args:
      - "run"
      - "--directory"
      - "/opt/vault-search-mcp"
      - "vault-search-mcp"
      - "--vault"
      - "/vault"
      - "--db"
      - "/data/vault-search.db"
    allowed_tools:
      - "vault_search"
      - "vault_get_note"
      - "vault_recent"
      - "vault_tags"
      - "vault_folders"
      # vault_reindex と vault_stats は通常不要 — 必要時のみ追加
```

### Docker 内で使う場合

```dockerfile
# Hermes の Dockerfile に追加
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY vault-search-mcp /opt/vault-search-mcp
RUN cd /opt/vault-search-mcp && uv sync --frozen
```

Hermes config.yaml:
```yaml
mcp_servers:
  vault-search:
    command: "uv"
    args: ["run", "--directory", "/opt/vault-search-mcp", "vault-search-mcp", "--vault", "/vault", "--db", "/data/vault-search.db"]
```

### obsidian-headless との組み合わせ（Docker Compose）

```yaml
services:
  obsidian-sync:
    image: node:22-slim
    command: >
      sh -c "npm install -g obsidian-headless &&
             ob login &&
             ob sync-setup --vault 'VaultName' &&
             ob sync --continuous"
    volumes:
      - vault-data:/vault

volumes:
  vault-data:
```

Hermes のコンテナで vault-data をマウントし、vault-search-mcp が同じボリュームを `/vault:ro` で参照する構成。

## パフォーマンス実績

877 ノートの Vault で測定:

| 指標 | 値 |
|---|---|
| 初回インデックス構築 | 4.2 秒 |
| FTS5 検索 (Tier 2) | 2-6 ms |
| キャッシュヒット (Tier 0) | < 0.1 ms |
| DB サイズ | 16 MB |

## 設計思想

Hermes Agent の Three-Layer Memory (Session/Persistent/Skill) と同じ SQLite + FTS5 基盤を採用。ByteRover の 5段階プログレッシブ検索のうち、LLM 呼び出し不要な Tier 0-2 を実装。「重要なものを見つけること」が本質であり、全文を詰め込むことではない。

## 将来の拡張候補

- **Tier 3**: LLM によるクエリ最適化・リランキング
- **Tier 4**: マルチターンエージェンティック検索（ASMR 的アプローチ）
- **Wikilink グラフ**: `[[リンク]]` の関連ノート発見
- **AKL**: ByteRover 式 Adaptive Knowledge Lifecycle（重要度スコア・成熟度）
