# Build stage: install dependencies with uv, then the project itself
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first so this layer is cached across source changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage: slim image with only the virtualenv
FROM python:3.12-slim-bookworm

WORKDIR /app

# UID 1000 matches the default first user on most Linux hosts, so the
# bind-mounted data/ and logs/ directories stay writable on a VPS
RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home bot \
    && mkdir -p /app/data /app/logs \
    && chown -R bot:bot /app

COPY --from=builder --chown=bot:bot /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

USER bot

ENTRYPOINT ["grid_trading_bot"]
CMD ["run", "--config", "config/config.json", "--no-plot"]
