# syntax=docker/dockerfile:1
#
# Multi-stage build for `recon.api.main`. `docs/contracts.md` section 5.
#
# Runs on python:3.12-slim rather than distroless: distroless's Debian 12 base
# ships Python 3.11, and this project requires >=3.12 (see
# docs/adr/0011-container-base-image-and-size.md). No build tools land in the
# runtime stage - the venv built here is copied in as-is.
#
# Measures ~262MB (docs/deployment.md), under the 300MB target named in
# issue #15 - despite claude-agent-sdk bundling a native `claude` CLI binary
# inside its own wheel and pyproject.toml's full dependency set (including
# langgraph/langchain-anthropic, needed by `recon.cli` but not by this API
# image) shipping unchanged. See the ADR above for how that was measured.

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer cached separately from source so editing src/ doesn't bust it.
# README.md is required here too: pyproject.toml's readme= field means
# hatchling reads it during the build, even for a deps-only sync.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
# --no-editable: an editable install's .pth redirect back to src/ is a dev
# convenience, not what belongs in an immutable image.
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system --gid 1000 recon \
    && useradd --system --uid 1000 --gid recon --home-dir /home/recon \
       --create-home --shell /usr/sbin/nologin recon

WORKDIR /app

COPY --from=builder --chown=recon:recon /app/.venv /app/.venv
# Read via relative Path("config/...") / Path("prompts/...") from CWD by
# runtimes/agent_sdk.py et al, not as packaged resources - WORKDIR must match
# at build and run time.
COPY --chown=recon:recon config/ config/
COPY --chown=recon:recon prompts/ prompts/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/recon

USER recon

EXPOSE 8000

# No curl in the slim image; urllib avoids adding one just for this.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/healthz', timeout=3)" || exit 1

ENTRYPOINT ["uvicorn", "recon.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
