FROM python:3.12-slim

# uv インストール
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /opt/vault-search-mcp

# 依存だけ先にインストール（レイヤーキャッシュ活用）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# ソース追加 + プロジェクトインストール
COPY src/ src/
RUN uv sync --frozen

# Vault マウントポイント
VOLUME /vault
# DB 永続化
VOLUME /data

ENV VAULT_ROOT=/vault
ENV VAULT_SEARCH_DB=/data/vault-search.db
ENV VAULT_SEARCH_LOG_LEVEL=INFO

ENTRYPOINT ["uv", "run", "vault-search-mcp", "--vault", "/vault", "--db", "/data/vault-search.db"]
