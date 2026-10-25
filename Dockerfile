# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first, so source edits don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim
# DejaVu for the captions on comparison sheets.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FRAME_ART_DATA=/data \
    FRAME_ART_DEST=/out \
    FRAME_ART_PORT=8740
# Unprivileged: the output folder sits inside Home Assistant's config, and the
# job fetches from the public internet. The host chowns /data and /out to 1000.
USER 1000:1000
EXPOSE 8740
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8740/healthz', timeout=4)"]
CMD ["frame-art", "serve"]
