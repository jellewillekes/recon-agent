# ADR 0007: Multi-agent mode — separate query() calls, client-side tool restriction

Status: Accepted
Date: 2026-09-07

## Context

Issue #12 (Step 7) asks for a multi-agent mode — a supervisor that decomposes and routes, two
workers each with their own tool subset, a critic that checks the conclusion against evidence
— explicit that tool restriction "has to be structurally real, not a suggestion," with a test
requirement that a worker's excluded tool is "structurally absent from that role's client, not
just refused at the prompt level."

Two design questions this ADR settles, researched before writing any code:

**Orchestration mechanism.** `claude_agent_sdk`'s `ClaudeAgentOptions` has a built-in `agents=`
field (`dict[str, AgentDefinition]`) that lets one CLI session spawn named subagents, each with
its own `tools`/`mcpServers`, invoked via an `Agent` tool the model calls itself. This is
genuinely structural — the CLI enforces the child's tool list, not the model's prompt-following.
The alternative is what this repo already does for single mode: build a `ClaudeAgentOptions`
per role and drive each with its own `query()` call, restricted via `allowed_tools`.

Both are structurally enforced. Chose **separate `query()` calls, orchestrated in our own
Python**, not `agents=`, because:
- Routing then lives in code we write and test directly, not inside an opaque CLI-mediated
  delegation the supervisor has to *decide* to invoke correctly.
- Each role's transcript, tool set, and cost stay independently observable — matches
  `CLAUDE.md`'s "report tokens and cost" more cleanly than one merged session.
- The critic can get zero MCP tools and zero `mcp_servers` at all — not achievable as cleanly
  inside one shared session where the parent still has its own tool surface.
- `allowed_tools` is the exact mechanism already audited and tested for single mode
  (`_build_options`, `ALLOWED_TOOLS`) — extending it per-role reuses proven code instead of a
  second, parallel enforcement path.

**Restriction layer.** Considered reaching into `tools/mcp_server.py` so a role's MCP subprocess
never registers its excluded tools (server-side), versus staying client-side
(`ClaudeAgentOptions.allowed_tools`/`mcp_servers`). Went with **client-side only**:
`allowed_tools` is CLI-enforced — the same mechanism that has denied every out-of-allowlist tool
call in this repo's own GitHub Actions runs — not a prompt convention, and it's the only version
directly unit-testable without a live model: assert on the constructed
`ClaudeAgentOptions.allowed_tools`/`mcp_servers` for a role (see
`tests/test_multi_agent.py::test_worker_options_exclude_tools_outside_subset` and
`::test_supervisor_and_critic_get_no_mcp_server_at_all`). Supervisor and critic get no
`mcp_servers` attached at all, not just an empty `allowed_tools` — the strongest form of
"structurally absent," reserved for the two roles that need zero tools by design.

## Decision

Four roles (`config/roles.yaml`), four `query()` calls per case, orchestrated by
`runtimes/multi_agent.py::run_multi_async`:

1. Supervisor decomposes the question into 1+ subtasks routed to `worker_lookup` or
   `worker_facts`.
2. Each routed worker runs with its own restricted `allowed_tools`, reporting findings +
   evidence.
3. Supervisor synthesizes a final answer from the workers' findings, reusing single mode's
   existing `_ANSWER_SCHEMA` (answer/evidence/confidence) — this is the piece that has to match
   `AgentResult`, so reuse rather than invent a second shape.
4. Critic checks the synthesis against its evidence. **No retry loop on rejection** — instead,
   `confidence` is forced to `"low"`. A rejection is a legitimate outcome for the harness to
   score, not a bug to engineer around, and a retry loop would add real, unbounded extra cost
   for a step where "if single wins, publish that" is the explicit stance the issue itself takes.

`runtimes/agent_sdk.py`'s single-query machinery (`_run_query`, tool-call extraction, oversized-
result recovery, cost conversion) was refactored to be schema/prompt-generic so both single mode
and every multi-agent role share one tested implementation, rather than duplicating it.

## Consequences

- A multi-mode case costs roughly 4-5x single mode's per-case Agent SDK spend (one decompose
  call, N worker calls, one synthesis call, one critic call, vs. single mode's one call) — real
  when actually run, per `CLAUDE.md`'s Cost section.
- `tool_calls` in the final `AgentResult` carry no per-role attribution (`ToolCall` has no role
  field, and adding one would be a `docs/contracts.md` change beyond this issue's scope) — they
  are the workers' calls only, flattened in call order. Supervisor/critic contribute no tool
  calls by construction.
- A critic rejection is only visible as `confidence == "low"` — there's no separate field
  recording *why* it rejected beyond that. Acceptable for this step; a future step wanting the
  critic's `reason` surfaced would need a contract change, not a code-only fix.
- Adding a third worker or changing a worker's tool subset is a `config/roles.yaml` edit, not a
  code change — the restriction mechanism generalizes beyond the two workers implemented here.
