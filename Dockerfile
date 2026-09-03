# Cloud Run Jobs image for the settlement gateway.
#
# Two stages so the runtime carries no build tooling. The result runs the CLI:
# `collect` and `sweep` are separate Jobs sharing this image and differing
# only in the command argument.

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

# gnupg encrypts every file we send to central systems. Elexon's
# communications team confirmed the requirement is compatibility with XSec,
# not XSec itself, and supplied the equivalent gpg parameters.
#
# Those parameters are deprecated: SHA1 digest, CAST5 cipher, 1024-bit RSA.
# gpg refuses them without --allow-old-cipher-algos, and each release narrows
# what is permitted further. Central Services' key was generated in 2008 and
# cannot be changed, so the constraint is theirs and permanent.
#
# The version is therefore worth pinning rather than taking whatever the base
# image ships: a silent gpg upgrade is a silent loss of the ability to talk to
# Elexon, and it would surface at Gate Closure.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gnupg \
 && rm -rf /var/lib/apt/lists/* \
 && gpg --version | head -2

# Non-root. Nothing here writes outside the keyring directory and the archive
# bucket, and the latter is reached over the network.
RUN useradd --create-home --uid 1000 gateway

WORKDIR /app
COPY --from=build --chown=gateway:gateway /app /app

# The keyring lives here, populated at startup from Secret Manager. Mode 700
# because gpg refuses to use a home directory that others can read, and the
# failure message when it does is not obvious.
RUN mkdir -p /home/gateway/.gnupg \
 && chown gateway:gateway /home/gateway/.gnupg \
 && chmod 700 /home/gateway/.gnupg

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONSUS_GNUPGHOME=/home/gateway/.gnupg

USER gateway

# No default command. Each Job supplies its own, so an image started by
# accident does nothing rather than doing the wrong thing.
ENTRYPOINT ["consus-settlement"]