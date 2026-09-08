# Hermes service image. beets is NOT in this image; it runs in its own pod (PLAN.md A2).
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini config.example.yaml LICENSE ./
COPY hermes ./hermes
RUN uv sync --frozen --no-dev

ENV PATH="/opt/venv/bin:$PATH" \
    HERMES_DATA_DIR=/data \
    HERMES_CONFIG_PATH=/config/config.yaml

RUN useradd --uid 1000 --create-home hermes && mkdir -p /data /config && chown hermes /data
USER hermes
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status < 500 else 1)" || exit 1
CMD ["hermes", "serve"]
