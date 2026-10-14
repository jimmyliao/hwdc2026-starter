# 僅供本機測試用。正式部署請只使用 deploy/Dockerfile.example（含預檢）。
# 基底映像標籤固定；digest 待部署時填入（`@sha256:<DIGEST_TO_FILL>`，不得捏造）。
FROM ghcr.io/astral-sh/uv:0.11.17-python3.11-bookworm-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY gateway/ gateway/
COPY server/ server/
COPY questions.sample.json ./
RUN uv sync --frozen --no-dev --extra server --extra gemini --no-install-project \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/app/.venv/bin:$PATH"
ENV PORT=8080
USER 10001:10001
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port \"$PORT\" --no-proxy-headers"]
