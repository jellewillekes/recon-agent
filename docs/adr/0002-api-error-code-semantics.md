# ADR 0002: Keep the 400/422 split inverted from FastAPI's default

Status: Accepted
Date: 2026-09-07

## Context

`docs/contracts.md` §5 specifies `POST /investigate` returns `400` for invalid input
("with the offending field named") and `422` for a schema-valid but unprocessable body.
Implementing the API service (Step 6, PR #31) required a custom `RequestValidationError`
handler to get this: FastAPI's own default is the reverse — 422 for any body that fails
Pydantic schema validation, regardless of whether the problem is a missing/mistyped field
or a semantically bad value. PR #31's round-2 review flagged the split as worth confirming
explicitly, since it diverges from what most FastAPI codebases do.

## Decision

Keep it as implemented: 400 for schema-invalid input (missing/mistyped fields — handled by
overriding FastAPI's `RequestValidationError` handler), 422 reserved for a schema-valid
body this build can't process (empty `question`, unsupported `mode`/`runtime`). This matches
`docs/contracts.md` §5 exactly, which predates this PR and is the binding module-boundary
contract per `CLAUDE.md`.

## Consequences

`src/recon/api/main.py` keeps its custom `_on_validation_error` handler rather than relying
on FastAPI's default. Any future endpoint added to `src/recon/api/` should follow the same
400/422 split for consistency with this one, not FastAPI's convention. If this is ever
worth revisiting, it's a contract change — update `docs/contracts.md` §5 in the same PR,
per `CLAUDE.md`'s rule for contract changes.
