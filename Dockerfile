# Cloud Run Jobs image for the settlement gateway.
#
# Two stages so the runtime carries no build tooling. The result runs the CLI:
# `collect`, `sweep` and eventually `reconcile` are separate Jobs sharing this
# image and differing only in the command argument.

FROM python:3.12-slim AS build

# uv resolves and installs from the lockfile, so the image matches what the
# tests ran against rather than whatever the index offers today.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev


FROM python:3.12-slim

# Non-root. Nothing here needs to write outside the archive bucket and the
# database, both of which are reached over the network.
RUN useradd --create-home --uid 1000 gateway

WORKDIR /app
COPY --from=build --chown=gateway:gateway /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER gateway

# No default command. Each Job supplies its own, so an image started by
# accident does nothing rather than doing the wrong thing.
ENTRYPOINT ["consus-settlement"]