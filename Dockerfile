# The image of HR-Sync, built and pushed to the GHCR by .github/workflows/docker-release.yml for every release tag.
# How to run it on a server: section "Operation" in the README and deploy/compose.yaml.

ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# hatch-vcs takes the version from the git tag, but the build context has no .git, so the workflow passes it
ARG VERSION=0.0.0+local
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
WORKDIR /app
# the dependencies first, so that their layer is reused as long as uv.lock doesn't change
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY README.md ./
COPY src ./src
# not editable, so that the image needs only the virtual environment, not the sources
RUN uv sync --locked --no-editable

FROM python:${PYTHON_VERSION}-slim
# without the zone files, TZ=Europe/Berlin would silently fall back to UTC
RUN apt-get update \
    && apt-get install --yes --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home hr-sync
# HR-Sync works with today's date: under UTC, 0:00 in Germany would still be the day before
ENV TZ=Europe/Berlin \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
COPY --from=builder /app/.venv /app/.venv
WORKDIR /app
USER hr-sync
ENTRYPOINT ["hr-sync"]
