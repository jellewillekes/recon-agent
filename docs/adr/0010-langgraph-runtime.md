# ADR 0010: LangGraph runtime — model access, MCP bridge, tool restriction, cost

Status: Accepted
Date: 2026-09-08

## Context

Issue #14 (Step 9) asks for `runtimes/langgraph.py` behind the same `Runtime` protocol as
the SDK runtime, against the same MCP server, "the largest gap against the target role."
Four decisions this ADR settles — two made before writing code, two forced by real
compatibility issues discovered only once the dependencies were actually installed.

**Model access needs a real `ANTHROPIC_API_KEY` — decided with the user directly, not a
unilateral call.** LangGraph drives its own model calls (send messages, get a response or a
tool-call request, loop itself around that). `claude_agent_sdk.query()` owns its *entire*
agentic tool-calling loop internally and has no clean "just one raw turn" primitive to wrap
as a LangChain chat model — every graph step would need a fresh CLI subprocess with no way
to get just one turn out of it, slow and architecturally awkward for uncertain benefit.
`langchain_anthropic.ChatAnthropic` is what LangGraph's model integration actually expects.
This means a second, separately-billed cost source alongside the Agent SDK subscription
credit `runtimes/agent_sdk.py` uses exclusively — Step 0 originally required
`ANTHROPIC_API_KEY` to be *empty*; that constraint now applies only to the SDK runtime.

**`mcp` is pinned below 2.0 project-wide — forced, not chosen freely.** The plan assumed
`langchain-mcp-adapters` would bridge the *existing* MCP server unchanged. Installing it
against this project's `mcp==2.1.1` failed immediately (`ImportError` on a class moved/
renamed between `mcp` 1.x and 2.x) — confirmed via PyPI metadata: `langchain-mcp-adapters`
(even its latest release) declares `mcp<2.0.0,>=1.24.0`, and has not been updated for `mcp`
2.x's breaking changes. Chose to downgrade `mcp` project-wide (`mcp>=1.30.0,<2.0.0`) over
writing a custom bridge on `mcp`'s own low-level client (`mcp.ClientSession`, already used
by `api/health.py`) — the user's call, since `claude-agent-sdk`'s own declared range
(`mcp<3.0.0,>=1.23.0`) still accommodates a 1.x pin, and this keeps one bridge library
instead of two ways of talking to the tool server. **Real consequence**: `mcp.server.
mcpserver.MCPServer` (the `>=2.0` class `tools/mcp_server.py` was built against) doesn't
exist in `mcp<2.0` — ported to `mcp.server.fastmcp.FastMCP`, the pre-2.0 name for the same
thing. Same `@server.tool()` decorator shape; confirmed via the full existing test suite
(240 tests, unrelated to this change) staying green after the port, plus a live check that
`mcp.ClientSession`/`stdio_client` (`api/health.py`'s primitives) still import fine.

**Tool restriction: filter `get_tools()`'s flat list against `config/roles.yaml`, same
config as ADR-0007.** `client.get_tools()` returns every MCP tool as one list, including
`flag_case_for_review_tool` — no per-connection scoping like the SDK's `allowed_tools`.
Restriction is a Python-side filter (`{f"{name}_tool" for name in role_config["tools"]}`)
before binding, so a role's node is only ever bound to its subset — the model literally
cannot request a tool outside it, enforced by the LLM API's own tool-calling contract
instead of the CLI's `allowed_tools`. No new role/tool config, per the issue's explicit
instruction not to fork a second one. (Only relevant starting multi mode, issue #14 part 2 —
single mode gets every tool, matching the SDK runtime's own single-mode `ALLOWED_TOOLS`.)

**Cost: a static per-model pricing table, same philosophy as `usd_to_eur_rate`.**
`ChatAnthropic` reports token counts (`AIMessage.usage_metadata`) but never a computed
dollar cost the way `claude_agent_sdk`'s `ResultMessage.total_cost_usd` does. Added
`config/models.yaml`'s `pricing:` table (currently just `claude-sonnet-5`: $2/$10 per Mtok
input/output, sourced from `platform.claude.com/docs/en/about-claude/pricing` on
2026-09-08) — static and manually updated, bump deliberately if pricing changes, exactly
`usd_to_eur_rate`'s existing convention.

## Decision

`src/recon/runtimes/langgraph.py`: `LangGraphRuntime` implementing `Runtime`, single mode
(this file) built on `langgraph.prebuilt.create_react_agent` — "a ReAct graph" in
LangGraph's own vocabulary, not hand-rolled (unlike ADR-0007's `agents=`-vs-manual call,
there's no stated reason here to avoid the maintained primitive). `response_format=
AnswerResponse` (a Pydantic mirror of `agent_sdk._ANSWER_SCHEMA`) gets the same validated
final structure the SDK runtime produces. `InMemorySaver` as the checkpointer for now (no
interrupt needed until multi mode's `flag_case_for_review`, issue #14 part 2, which is
also where a real `AsyncPostgresSaver` lands). `--runtime {sdk,langgraph}` on `run`/`eval`
(default `sdk`, so nothing existing changes), dispatched through `cli._build_runtime`.

`investigator.max_turns` (`config/models.yaml`, already read by the SDK runtime) is reused
as `recursion_limit` rather than forking a second turn-budget knob — an approximation, not
an exact equivalent: each tool-call round trip is ~2 graph steps in the prebuilt ReAct
graph, so this is generously matched, not precisely. Still a real cost-safety bound, which
is what `max_turns` exists for in the first place.

`run_budget` (`max_tool_calls`/`max_tokens`/`max_wall_clock_s`, same section `agent_sdk.py`
reads) now applies here too, at the same fidelity `agent_sdk.py`'s own single mode has.
`max_wall_clock_s` wraps the whole run in `asyncio.wait_for` as a live, hard ceiling.
`max_tool_calls` is checked after every graph step via `astream`'s `stream_mode="values"`
(which yields the accumulated state after each node, unlike `ainvoke`, which only returns
once the whole run is over) — a breach raises `_BudgetExceeded` and closes the stream
immediately, the same live fidelity `agent_sdk._run_query`'s streaming `_BudgetTracker` has
for tool calls. `max_tokens` is still checked once the run completes, same "reported after
the fact" pattern `agent_sdk.py`'s single mode already uses for tokens (there, usage is only
known once a call's `ResultMessage` arrives; here, once a breaching message's own
`usage_metadata` has already been folded into the running total). Round 1 review of this PR
found `run_budget` entirely bypassed at merge time; round 2 found the initial `max_tool_calls`
fix still only checked post-hoc — both closed now, to the same rigor `agent_sdk.py` already
has for tool calls and wall-clock, plus its own existing gap for tokens.

**Known, accepted gap:** `create_react_agent` is deprecated as of LangGraph 1.0 in favor of
`langchain.agents.create_agent` (removal planned for 2.0). Using it anyway rather than
adding a sixth dependency (`langchain`) for a warning, not a removal, on a currently-working
API — revisit at the next LangGraph major-version bump.

## Consequences

- Every LangGraph run now costs real, metered `ANTHROPIC_API_KEY` spend on top of whatever
  Agent SDK subscription credit the SDK runtime already uses — CLAUDE.md's Cost section
  applies to both now, not just the SDK path.
- `mcp<2.0` is now a project-wide constraint, not just this runtime's — anything depending
  on `mcp`'s 2.x-only API surface would need re-porting the way `tools/mcp_server.py` was.
  Revisit if `langchain-mcp-adapters` ships 2.x support.
- Tool-call cost accuracy for the LangGraph runtime depends on `config/models.yaml`'s
  `pricing:` table staying current by hand — unlike the SDK runtime, there's no live
  source of truth to fall back on if it drifts.
- `_extract_tool_calls`'s tool-name allowlist (`_TOOL_NAMES`) has to be kept in sync with
  `tools/mcp_server.py`'s actual registered tools by hand; a new tool added there without a
  matching entry here would silently never show up in `AgentResult.tool_calls`.
