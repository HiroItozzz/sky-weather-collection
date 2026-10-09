# Cloud Run 用のイメージ。server/ をビルドコンテキストにする。
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# 依存パッケージだけ先に入れて、コードの変更でキャッシュが無駄にならないようにする
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# root で動かさない
RUN useradd --create-home --uid 10001 app
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    SKY_BACKEND=gcp
# Cloud Run は待ち受けるポートを PORT で渡す
CMD ["sh", "-c", "exec uvicorn sky_server.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
