# Voice examples

Worked examples for the Voice rules in `AGENTS.md`. They moved here to keep the
always-loaded instructions short; the rules themselves stay in `AGENTS.md`.

Before, from an actual review comment on this repo:

> fetch_csv treats "a file exists at dest" as "cache is valid," full stop —
> it never checks that cache against PINNED_COMMIT. But docs/data-sources.md
> explicitly documents the workflow as "bump the pin deliberately, in its own
> PR, if the upstream file changes," which implies that bumping the constant
> is how you get fresh data. In practice: a developer or CI runner that
> already has data/raw/finance_agent_bench/public.csv cached from before a
> future pin bump will keep silently serving the stale file — nothing in
> this code path ever notices the pin moved, and there's no error, warning,
> or log.

After:

> fetch_csv only checks whether dest exists — it never checks that file
> against PINNED_COMMIT. After a future pin bump, anyone with an old cached
> file keeps serving it silently. No error, no warning.

A review body with several findings, each anchored inline, uses the body only
for the summary and status list:

> Round 2. Core mechanism still sound.
>
> **Open:** tool_calls ordering under parallel tool use (agent_sdk.py:151)
> **Resolved:** max_turns bound, dead pricing config
> **New:** `_find_case`'s not-found branch has no test (cli.py:54)
>
> Inline comments have the specifics.

Each inline comment on the lines above stays to one bullet:

> **Blocking:** `_parse_tool_result`'s list-branch is untested — every
> fixture in the suite uses the string path. `tool_calls` feeds the eval
> harness, so a silent fallback to `status="unavailable"` here would go
> unnoticed.
