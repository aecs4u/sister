# syntax=docker/dockerfile:1.4

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    git \
    curl \
    ca-certificates \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON=/usr/local/bin/python3

WORKDIR /app

ARG GITHUB_TOKEN=""
ARG GAR_TOKEN=""
ARG GAR_REGION=europe-west1
ARG GAR_PROJECT=apps-aecs4u
ARG GAR_REPOSITORY=python-packages

RUN if [ -n "$GITHUB_TOKEN" ]; then \
    git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; \
    fi

ENV UV_EXTRA_INDEX_URL=${GAR_TOKEN:+"https://oauth2accesstoken:${GAR_TOKEN}@${GAR_REGION}-python.pkg.dev/${GAR_PROJECT}/${GAR_REPOSITORY}/simple/"}

COPY pyproject.toml uv.lock README.md ./

RUN uv venv /app/.venv && \
    . /app/.venv/bin/activate && \
    uv sync --frozen --no-dev --no-install-project

RUN git config --global --remove-section url."https://${GITHUB_TOKEN}@github.com/" 2>/dev/null || true

COPY sister/ ./sister/
COPY alembic/ ./alembic/
COPY alembic.ini ./

RUN . /app/.venv/bin/activate && uv sync --frozen --no-dev

FROM ${BASE_IMAGE} AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    curl \
    ca-certificates \
    libffi8 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/tini /sbin/tini

RUN useradd -m -u 1000 -U appuser

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:${PATH}" \
    VIRTUAL_ENV=/app/.venv \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/sister /app/sister
COPY --from=builder --chown=appuser:appuser /app/alembic /app/alembic
COPY --from=builder --chown=appuser:appuser /app/alembic.ini /app/
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml /app/

RUN /app/.venv/bin/python -m playwright install --with-deps chromium && \
    mkdir -p /app/data /app/logs /app/outputs /ms-playwright && \
    chown -R appuser:appuser /app /ms-playwright && \
    SITE_PACKAGES=$(ls -d /app/.venv/lib/python*/site-packages | head -1) && \
    printf "#!/bin/sh\nexport PYTHONPATH=%s:/app\nexec python -m uvicorn sister.main:app --host 0.0.0.0 --port 8080 --workers 1\n" "$SITE_PACKAGES" > /app/entrypoint.sh && \
    chmod +x /app/entrypoint.sh

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/app/entrypoint.sh"]
