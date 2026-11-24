# syntax=docker/dockerfile:1
FROM python:3.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /srv

# Install dependencies first (cached unless the lockfile or optimade submodule change).
COPY pyproject.toml uv.lock ./
COPY deps/optimade-python-tools deps/optimade-python-tools
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the application itself.
COPY app app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

COPY optimade_config.json /srv/optimade_config.json

ENV PATH="/srv/.venv/bin:$PATH" \
    OPTIMADE_CONFIG_FILE=/srv/optimade_config.json \
    OPTIMADE_LOG_DIR=/tmp

RUN useradd --system --uid 1000 optimade
USER optimade

EXPOSE 8000
# One worker per container; scale with replicas. Override with e.g. `--workers 4`.
CMD ["fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
