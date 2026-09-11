# ADR 0011: Container base image, and shipping the full dependency set

Status: Accepted
Date: 2026-09-11

## Context

Issue #15 (Step 10) asks for `docker/Dockerfile` on "distroless or slim," non-root,
no build tools in the final layer, image under 300MB.

**Base image.** distroless's Debian 12 variant ships Python 3.11; `pyproject.toml`
requires `>=3.12`. Matching versions on distroless would mean building CPython 3.12 from
source or vendoring shared libraries into `gcr.io/distroless/cc-debian12` — real effort
for a step whose actual goal is containerizing the existing service, not re-deriving a
Python base image. `python:3.12-slim-bookworm` gives the exact interpreter version this
project already runs on, still without a package manager's worth of build tooling once
the multi-stage build only copies the finished venv into it.

**Size — initial concern, corrected by actually building and measuring.** Inspecting the
dependency tree before building anything raised a real worry: `claude-agent-sdk` bundles a
native `claude` CLI binary inside its own per-platform wheel
(`claude_agent_sdk/_bundled/claude`) — good news functionally, no separate Node/npm
install step needed, `claude_agent_sdk._find_bundled_cli()` finds it directly — but on
disk, unpacked, that binary alone is ~190-207MB, and `polars` is a main dependency
imported nowhere in `src/recon` (confirmed via `grep -rn polars src/recon`, zero hits;
~200MB unpacked with its native runtime). The unpacked `.venv` in the built image is
~660MB. Summed on-disk sizes made "under 300MB" look unreachable without a
`pyproject.toml` change.

Building the actual image and measuring it (`docker inspect recon-agent:dev --format
'{{.Size}}'`, matching what `docker images` reports and what a registry pull would
transfer) told a different story: **262MB**, under the ticket's target. The bundled CLI
binary and the native runtime libraries are highly compressible, so the gap between the
~660MB unpacked venv and the 262MB distributable image is real, not a measurement error —
confirmed by inspecting `/app/.venv` inside a running container
(`docker run --rm --entrypoint sh recon-agent:dev`) and cross-checking against `docker
history`. Measured on this machine's native architecture (arm64, via Rancher Desktop); an
amd64 CI build should land in the same order of magnitude given the wheel sizes are
comparable across platforms in `uv.lock`, but hasn't been measured directly (CI docker
builds are step 11, out of scope here).

`polars` and `pytest`/`ruff`-as-main-deps are still real, fixable bloat (a
`pyproject.toml` optional-dependencies split would shrink this further), but with the
actual target already met, that's a nice-to-have, not something blocking this step.

## Decision

`docker/Dockerfile` builds on `python:3.12-slim-bookworm` for both stages, non-root user,
multi-stage (builder installs via `uv sync --frozen --no-dev --no-editable`, only the
resulting `.venv` — no compiler, no `uv` binary itself — is copied into the runtime
stage). The dependency set ships unchanged from what `uv sync` already installs today; no
`pyproject.toml` edits in this PR — the size target is met without needing them.

## Consequences

- The built image measures 262MB, under the 300MB target, without any dependency
  surgery. `docs/deployment.md` records the measured figure and how it was taken, so a
  future re-measurement (a new dependency, a base image bump) has something concrete to
  compare against.
- `polars` (unused in `src/recon`) and `pytest`/`ruff` (main deps despite being dev-only
  tools) are still worth a follow-up issue proposing a `pyproject.toml`
  optional-dependencies split — real, if now non-urgent, cleanup — separate review, not
  bundled into this one.
- If a future step needs distroless specifically (a stricter security posture), it will
  need either a Python-version-matched custom base or dropping to whatever Python version
  distroless's Debian release actually ships — revisit then, not speculatively now.
