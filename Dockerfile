# syntax=docker/dockerfile:1

# ── Base stage: locked runtime dependencies ───────────────────────────
FROM python:3.13-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /bin/

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# HTTPS model providers and the remote PostgreSQL archive use TLS.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached unless pyproject.toml / uv.lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Include the complete workbench: metrics, archive SQL, templates and JS/CSS.
# Explicit copies keep local databases, reports and credentials out of the image.
COPY README.md alembic.ini ./
COPY finbot/ ./finbot/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY docker/ ./docker/
RUN uv sync --frozen --no-dev

# Normalize host permissions (including files copied from external macOS volumes).
# The experiment database and its encryption key share the persistent data volume.
RUN chmod -R a+rX /app && \
    mkdir -p data uploads cache/share_cards certs && \
    useradd --create-home --shell /bin/bash finbot && \
    chown -R finbot:finbot /app

EXPOSE 8000
# Liveness only: archive/TLS/schema diagnostics are available via archive_cli check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).close()"]
ENTRYPOINT ["sh", "docker/entrypoint.sh"]
# Starts both Gunicorn and the experiment worker; bootstrap runs in the entrypoint.
CMD ["python", "scripts/start_local.py", "--container"]

# ── Full target: includes Playwright + Chromium for OG image rendering
FROM base AS app-full

ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
RUN playwright install --with-deps chromium

RUN chown -R finbot:finbot /app/.playwright
USER finbot

# Keep the lean image as the default for both docker build and Compose.
# Chromium is optional; the analytics charts do not require it.
FROM base AS app
USER finbot
