# vault-search-mcp

Obsidian Vault の高速構造化検索を MCP (Model Context Protocol) で提供するサーバー。

## 特徴

- **FTS5 trigram** — 日英混在テキストの全文検索（外部トークナイザ不要）
- **3段キャッシュ** — ByteRover 式プログレッシブ検索 (Tier 0: ~0ms, Tier 1: ~1ms, Tier 2: ~10ms)
- **ファイル監視** — watchdog による差分インデックス更新
- **frontmatter 対応** — タグ・日付・フォルダでの構造化フィルタリング
- **ヘッドレス運用** — Obsidian アプリ不要、ファイルシステム直アクセス

## MCP ツール一覧

各ツールの返り値は Pydantic モデルで定義されており、FastMCP が生成する JSON Schema にフィールド説明まで含まれる（`src/vault_search/schemas.py`）。

| ツール | 主要引数 | 説明 | 返り値型 |
|---|---|---|---|
| `vault_search` | `query`, `tags?`, `folder?`, `limit?`, `offset?`, `metadata_filter?` | 全文検索（タグ・フォルダ・frontmatter フィルタ付き） | `SearchResponse` |
| `vault_get_note` | `path` | 単一ノートの全文取得 | `NoteDetail`（未存在時 `NoteNotFoundError`） |
| `vault_recent` | `limit?`, `folder?` | 最近更新されたノート一覧 | `{"notes": list[RecentNote]}` |
| `vault_tags` | — | 全タグと出現回数 | `{"tags": list[TagCount]}` |
| `vault_folders` | — | フォルダ構造とノート数 | `{"folders": list[FolderCount]}` |
| `vault_reindex` | `force?` | インデックス再構築 | `ReindexStats` |
| `vault_stats` | — | インデックス統計情報 | `VaultStats` |

list を返すツール (`vault_recent` / `vault_tags` / `vault_folders`) は `{"notes": [...]}` / `{"tags": [...]}` / `{"folders": [...]}` の **envelope dict** で返す。FastMCP は裸の `list[T]` 戻り型に対して structured content を `{"result": [...]}` へ自動ラップしてしまうため、`schema://tools` が宣言する output_schema と実レスポンスを一致させる目的で envelope 統一している。

### `metadata_filter` パラメータ（frontmatter AND フィルタ）

`vault_search` のみ対応。frontmatter プロパティ値に対する AND 条件。

```jsonc
{
  "query": "memory",
  "metadata_filter": {
    "status": "active",                    // 暗黙の eq
    "priority": {"in": ["high", "medium"]},// list メンバーシップ
    "archived": {"ne": "true"}             // 否定
  }
}
```

対応演算子:

| 演算子 | 形式 | 意味 |
|---|---|---|
| `eq` (暗黙) | `"key": "value"` | 文字列一致。frontmatter 値が list の場合は「含む」判定 |
| `ne` | `"key": {"ne": "value"}` | 一致しない |
| `in` | `"key": {"in": ["a", "b"]}` | いずれかに一致（list 値 frontmatter では交差） |

キーは英数字 / `_` / `-` / `.` のみ、最大 64 文字（`validate_identifier`）。
値はすべて文字列として比較される（frontmatter の型はロード時に正規化）。

`schema://tools` の `vault_search.input_schema.properties.metadata_filter` では、この演算子 grammar が `additionalProperties.oneOf` で構造化公開されている。エージェントは MongoDB 風 `$in` や `{"eq": "..."}` ではなく上表の 3 形式のみを選ぶこと。

## MCP Resources

### `schema://tools`

AI エージェントが初回起動時に `read_resource` で取得することを想定した自己記述リソース。
ランタイムで実在する frontmatter キー一覧と全ツールの入出力スキーマを JSON で返す。

返り値構造:

```jsonc
{
  "tools": {
    "vault_search": {
      "description": "Vault 内のノートを全文検索する。...",
      "input_schema":  { /* JSON Schema: query, tags, folder, limit, offset, metadata_filter */ },
      "output_schema": { /* SearchResponse の JSON Schema */ }
    },
    "vault_get_note": { "description": "...", "input_schema": {...}, "output_schema": {...} },
    // ... 他のツールも同形式
  },
  "frontmatter_keys": ["status", "priority", "tags", "created", ...]  // Vault に実在するキーのみ
}
```

`frontmatter_keys` は DB から毎回列挙されるため、エージェントが存在しないキー名を想像（ハルシネーション）するのを防止する。
`metadata_filter` で使えるキーの正解セットとして参照する。

#### output_schema は `schema://tools` と MCP `tools/list.outputSchema` で同一 (rich schema)

ツール実装の戻り型は `dict[str, Any]` に統一している (FastMCP が Union 戻り型と list 戻り型に
対して `wrap_output=True` を自動設定し、structured content を `{"result": ...}` にラップして
しまう問題の回避)。list を返すはずの `vault_recent` / `vault_tags` / `vault_folders` も同じ
理由で envelope dict (`{"notes": [...]}` / `{"tags": [...]}` / `{"folders": [...]}`) を返す。

`dict` 戻り型のままだと FastMCP 自動生成の `outputSchema` は空 schema に退化するため、
登録後に `_TOOL_ENTRIES[<name>]["output_schema"]` を MCP `Tool.output_schema` へ差し戻し、
`schema://tools` と MCP `tools/list` の両方で同一の rich schema を公開している
(`server._inject_rich_output_schemas()`)。

### Breaking changes (Pydantic 移行)

- 全ツールが `dict` / `list[dict]` ではなく Pydantic モデル（`extra="forbid"`）を返すよう変更。MCP プロトコル経由ではシリアライズ後の JSON 形状はほぼ同一だが、未知フィールドの混入が拒否される。
- `vault_get_note` は未存在ノートに対して `{"error": "..."}` を返す代わりに `NoteNotFoundError` を送出する。FastMCP は MCP のエラーレスポンスへ自動変換する。
- `SearchResponse.tier` は `Literal[0, 1, 2]` に固定。
- **list 戻りツールの envelope 化 (breaking)**: `vault_recent` / `vault_tags` / `vault_folders` は従来の `list[...]` ではなく `{"notes": [...]}` / `{"tags": [...]}` / `{"folders": [...]}` の envelope dict を返す。FastMCP が list 戻り型を `{"result": [...]}` で自動ラップする挙動と `schema://tools` の output_schema 公開との drift を解消するための統一。

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

### 推奨初回フロー（self-describing startup）

Hermes（あるいは任意の MCP クライアント）側のエージェントは、vault-search-mcp に接続した直後に
1 回だけ `schema://tools` リソースを読み込み、返り値をセッションコンテキストに載せておくと、
以降の呼び出しで引数を外さずに済む。

```python
# 疑似コード
schema = await session.read_resource("schema://tools")
# schema["tools"]["vault_search"]["input_schema"]      -> どの引数が使えるか
# schema["tools"]["vault_search"]["output_schema"]     -> 返り値の構造
# schema["frontmatter_keys"]                           -> metadata_filter のキー候補

# 以降の呼び出しは schema に基づいて構築
await session.call_tool("vault_search", {
    "query": "memory architecture",
    "metadata_filter": {"status": "active"},  # frontmatter_keys に含まれるキーだけ使う
    "limit": 10,
})
```

この流れにより、(a) エージェントが存在しない frontmatter キーを想像するのを防ぎ、
(b) ツール引数・返り値のフィールド名をコードに埋め込まず、ランタイムスキーマから派生できる。

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

## For AI Agents

このサーバーは「Agent DX is predictability」を指針に設計されている。
エージェントの実装を書く・読む際に意識すべき不変条件 (invariants) を以下にまとめる。

### 推奨起動フロー

1. **最初に `schema://tools` を `read_resource`** する
   - 返り値の `tools.*.input_schema` / `output_schema` を以後のツール呼び出しの真実の情報源として扱う
   - `frontmatter_keys` を短期コンテキストにロードし、`metadata_filter` で使うキー名はここから必ず選ぶ
2. `vault_search` で `query` + `metadata_filter` を AND 組み合わせて絞り込む
3. 具体的なノート本文が必要になった時だけ `vault_get_note(path)` を叩く

### 不変条件 (Invariants)

- **AND 結合のみ**: `vault_search` の `query` / `tags` / `folder` / `metadata_filter` は全て AND。OR は非対応。OR が必要なら複数回呼ぶか `metadata_filter` の `in` 演算子を使う
- **`metadata_filter` のキー制約**: 英数字 / `_` / `-` / `.` のみ、最大 64 文字。違反時は `ValidationError`
- **`metadata_filter` の演算子**: `eq` (暗黙) / `{"ne": str}` / `{"in": list[str]}` のみ。範囲比較・正規表現は非対応
- **値は文字列比較**: frontmatter 値はロード時に文字列化され、`metadata_filter` の値も文字列前提で比較される
- **list 値 frontmatter の扱い**: frontmatter が list のキー (`tags` 等) に `eq` を指定すると「含む」判定になる
- **`schema://tools` はランタイム生成**: `frontmatter_keys` は Vault の現在状態から都度列挙する。キャッシュせず、起動時に毎回 `read_resource` することが推奨
- **`vault_get_note` の未存在パス**: `NoteNotFoundError` を送出（MCP エラーレスポンスに変換される）。`None` は返らない
- **Pydantic `extra="forbid"`**: 全返り値モデルは未知フィールドを拒否する。スキーマ準拠でコードを書くこと

この「正規の情報源」は `schema://tools` リソースと各ツールの docstring。README の記述と乖離がある場合は**ランタイムリソースを優先**する。
