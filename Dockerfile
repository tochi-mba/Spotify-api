# syntax=docker/dockerfile:1

# --- build -------------------------------------------------------------------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies are installed from the manifests alone, without the project
# itself, so this layer stays cached until the lockfile actually changes --
# editing source does not re-resolve or re-download anything.
COPY pyproject.toml uv.lock ./
# git: uv fetches the family's client packages from tagged git sources.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
# The token exists only for this RUN, in git's process environment, never a layer.
# Without a secret, public sources are fetched anonymously.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --frozen --no-install-project --no-dev

# Only now does the source arrive, so only this cheap layer rebuilds on a code
# change. README.md comes too because pyproject declares it as the readme and
# the build backend reads it.
COPY README.md ./
COPY src/ ./src/
# Not editable: the runtime stage copies the venv alone, so the package has to be in it.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --frozen --no-dev --no-editable

# --- runtime -----------------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    SPOTIFY_API_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8007

# Liveness only. /ready would mark the container unhealthy whenever Spotify is
# having a bad afternoon, and a container healthcheck should not mean that.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8007/healthy || exit 1

CMD ["python", "-m", "spotify_api"]
