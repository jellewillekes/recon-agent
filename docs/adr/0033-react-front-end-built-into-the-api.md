# 0033: A React front end, built into the API image

## Context

The workspace page (#117) was hand-written HTML and JavaScript in
`src/recon/api/static/`. #121 replaces it with a typed front end: React, TypeScript
and Vite in `web/`, with unit and browser tests, while still shipping one image and
running no Node at request time. Three things needed deciding: where the build goes,
how the client stays in step with the API, and how the browser tests avoid paid
agent calls.

## Decision

Decided by the user on 2026-10-08, except where noted:

- `npm run build` writes to `src/recon/api/static/`, which is gitignored. The
  Dockerfile builds it in a `node:22` stage and copies it into the Python build, and
  `pyproject.toml` declares it as a hatch artifact so the wheel keeps it. The runtime
  image has no Node.
- Without a build, `/` serves a short page saying to run `make web`. The API works in
  a Python-only checkout.
- The client's types are generated from `web/openapi.json` with openapi-typescript.
  `tests/test_api_web.py` fails when the committed schema differs from the API, and
  CI fails when the generated types differ from the schema. So a contract change
  breaks the front end's build, not the page. Endpoints that return a
  `JSONResponse` declare their model with `responses=` so the schema types them.
- The Playwright smoke tests run against `vite preview` with every API call answered
  from synthetic fixtures in `web/e2e/fixtures`. No agent runs and nothing costs
  credit. (Claude's choice.)
- Dependencies are the ones the user approved: react, react-dom, vite, typescript,
  vitest, @playwright/test, openapi-typescript, @types/react, @types/react-dom,
  eslint, typescript-eslint and eslint-plugin-react-hooks. TypeScript is held at 5.9,
  since openapi-typescript and typescript-eslint don't support 6 or 7 yet. Component
  tests render to markup in Node instead of adding jsdom.

## Consequences

- Working on the page needs Node 22 or later. Working on the API doesn't.
- An API contract change has two regeneration steps:
  `scripts/export_openapi.py`, then `npm run api:types`.
- The browser tests check the page against fixtures, not a running API. The API's
  own tests cover its responses, and the shared schema ties the two together.
- The run trace can't be grouped per worker in multi mode yet: `ToolCall` doesn't
  record which worker made the call. That needs a contract change of its own.
