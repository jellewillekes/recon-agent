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
fix still only checked post-hoc; round 3 found the wall-clock path discarding partial
telemetry the tool-call path preserved — all closed now, to the same rigor `agent_sdk.py`
already has for tool calls and wall-clock, plus its own existing gap for tokens.

**Found re-verifying round 3's fix, before merge:** the wall-clock breach path
(`_run_graph`'s `except TimeoutError`) missed a real failure mode. `asyncio.wait_for`'s own
timeout always raises a plain `TimeoutError` (confirmed directly against this project's own
graph) — but LangGraph's pregel engine runs on `anyio` structured concurrency internally,
and under load a cancellation landing mid-step can instead surface as a
`BaseExceptionGroup` ("unhandled errors in a TaskGroup") wrapping that same
`CancelledError`/`TimeoutError` — observed live, intermittently, running this project's own
test suite (not reproducible in isolation, only under full-suite load). The narrow `except
TimeoutError` missed this shape entirely, falling through to the generic exception handler
and silently losing the same partial telemetry round 3 had just fixed for the plain-
`TimeoutError` case. Fixed by also catching `BaseExceptionGroup`, splitting it via
`.split((asyncio.CancelledError, TimeoutError))` to confirm it's actually a
cancellation/timeout before treating it as a wall-clock breach — a group containing an
unrelated real error still propagates as itself, not mislabeled. Covered by two
deterministic tests exercising `_run_graph` directly with a fake graph, rather than
depending on the same timing race that made the bug intermittent in the first place.

**MCP subprocess teardown, round 2/3's open question — resolved, not just documented.**
No explicit teardown of `MultiServerMCPClient`/the MCP subprocess exists in `run_async`.
Verified directly against this project's installed `langchain-mcp-adapters` source (not
just its docs): both `get_tools()`'s discovery call and every individual bound tool's
execution (`convert_mcp_tool_to_langchain_tool`'s `call_tool`) scope their own subprocess
session inside `async with create_session(...)`, torn down via the context manager
protocol on success, exception, or cancellation alike. `MultiServerMCPClient` itself never
holds a persistent session to close — there is nothing to leak, including on the
`asyncio.wait_for` timeout path.

**Round 4 review of PR #46, two more findings, both fixed.** `_compute_cost_eur`'s pricing
lookup (`model_config["pricing"][model]`) had no guard — a `KeyError` for a model missing
a `pricing:` entry, raised from inside `run_async`'s `except _BudgetExceeded` handler,
would have been unrecoverable: a second `except` block can't catch a new exception raised
while handling the first, so this would have broken the "never raises" contract on nothing
worse than a config gap. Now returns `0.0` instead of raising — reported cost being wrong
beats the whole run crashing over it. Separately, `_run_graph`'s `BaseExceptionGroup`
handling (above) only checked whether a cancellation was present, not whether an *unrelated*
exception was bundled alongside it in the same group — that case silently discarded the
unrelated exception. Now included in the `_BudgetExceeded` reason text
(`"...; also: {other!r}"`) rather than dropped, while still degrading gracefully for the
genuine cancellation. Both covered by new tests.

**Known, accepted gap:** `create_react_agent` is deprecated as of LangGraph 1.0 in favor of
`langchain.agents.create_agent` (removal planned for 2.0). Using it anyway rather than
adding a sixth dependency (`langchain`) for a warning, not a removal, on a currently-working
API — revisit at the next LangGraph major-version bump.

## Multi mode (issue #14 part 2)

`src/recon/runtimes/langgraph_multi.py`, a hand-built `StateGraph`: `decompose` → `Send`-
fanned-out `worker` nodes (one per subtask, each `_build_react_subgraph` reused unchanged
from single mode) → `synthesize` → `critic` → a conditional edge to `confirm_flag` or `END`.
Same module split as ADR-0007's `agent_sdk.py`/`multi_agent.py`: shared streaming/budget/cost
primitives (`_run_graph`, `_build_react_subgraph`, `_build_checkpointer`, `_Outcome`) live in
`langgraph.py`, imported rather than duplicated — this module was already long (474 lines
before this PR) with exactly that machinery.

**Worker budget enforcement is coarser than single mode's, not equivalent.** Each worker now
gets its own `role_config["max_turns"]` as `recursion_limit` (fixed post-review — it was
previously unset, silently falling back to LangGraph's own default instead of
`config/roles.yaml`'s per-role value). That bounds a worker's own turns, but `_run_graph`'s
live `max_tool_calls` check only runs between supersteps of the *outer* graph — a `Send`-fanned
worker is one `.ainvoke()` call from the outer graph's perspective, so its internal tool calls
are invisible to that check until the worker's whole subgraph returns, not per tool call the
way single mode's own streamed loop is checked. A single stuck or looping worker can still spend
up to its `max_turns` ceiling before the run's tool-call budget gets a chance to stop it early.
Giving workers the same live, per-tool-call visibility single mode has would mean streaming each
worker's own subgraph and folding its state into the outer graph's live check mid-flight —
a real change to how `Send`-fanned workers execute, not a bug fix; out of scope here. The
`recursion_limit` fix above is the bound that exists today.

**Flag-interrupt design: a dedicated graph node, not a bound tool — decided with the user
directly.** The SDK runtime's supervisor (`multi_agent.py`, issue #13) drives the entire
dry-run/pause/confirm protocol itself via ordinary multi-turn tool-calling on
`flag_case_for_review_tool`, per `prompts/supervisor.md`'s explicit instructions — there is no
dedicated Python orchestration step for flagging in the SDK version at all. Replicating that
in LangGraph would mean binding the tool inside the supervisor's own `create_react_agent`
loop and calling `interrupt()` from inside that tool's implementation. Live-verified this has
a real caveat: **resuming an interrupted node re-executes that node's entire body from the
start** — risky for a node that might have other tool calls batched into the same turn.
Chose a separate node instead: `AnswerResponse` (the supervisor's synthesize-step schema)
gains an optional `flag_reason: str | None` field; a dedicated `confirm_flag` node, reached
only when it's set, does the pause. Needs its own prompt file, `prompts/supervisor_langgraph.md`
— the interaction protocol genuinely differs from `prompts/supervisor.md`'s (tool-calling
instructions vs. a plain field contract), so sharing one file would mean instructions for a
protocol that doesn't apply to whichever runtime is reading them. `prompts/supervisor.md`
stays untouched, SDK-only. `config/roles.yaml`'s supervisor entry still lists
`tools: [flag_case_for_review]` (unchanged, per the issue's explicit instruction not to fork
a second role config) but `langgraph_multi.py` never reads that key — a deliberate,
ADR-recorded divergence, not an oversight, from how the SDK runtime interprets the same file.

**`_confirm_flag_node`'s replay safety, and what that means for a paused `AgentResult`.**
Everything before `interrupt()` in the node — a `dry_run=True` preview call and an unconfirmed
call (for a `preview_token`) — never touches Postgres, so the node is safe to replay from the
start (the caveat above only bites when something before `interrupt()` has a side effect). The
actual write, if approved, happens once, after `interrupt()` returns. One consequence, confirmed
by test (`test_run_multi_pauses_when_supervisor_sets_flag_reason`): a node's return value is
only merged into `AgentState` once it *completes*, and an interrupted node never does — so the
paused `AgentResult` carries the real answer/evidence/confidence already produced by
decompose/workers/synthesize/critic, but **no** `flag_case_for_review` entries in `tool_calls`
yet, not even the dry-run preview's. All of them (2 if declined, 3 if approved) land at once,
after `resume`, when the node's one successful pass finally returns.

The confirm node calls `tools/review_flag.py::flag_case_for_review` **directly as a plain
Python function**, not through the MCP protocol/subprocess — it's graph-orchestrated code, not
model-driven, so there's no reason to spawn an MCP subprocess for a write nothing is "calling
as a tool." Same idempotency-key/`preview_token` protections issue #13 already built and
tested (ADR-0008), no second write path.

**No synchronous CLI approval UX in this PR** — the issue asks for the interrupt mechanism,
not a human-approval flow, and doesn't mention one. A paused run returns a real `AgentResult`
(`answer`/`evidence`/`confidence` from before the pause, `error` naming the `thread_id` needed
to resume) — reusing `error` for "ended early/differently than a clean success," the same
precedent `_BudgetExceeded` already established. `LangGraphRuntime.resume(case, thread_id,
approved) -> AgentResult` (outside the `Runtime` protocol, which only requires
`run`/`run_async`) completes a paused run via `Command(resume=approved)`. No CLI wiring for it
in this PR — tests call it directly; a CLI resume command is a natural, separate follow-up.

**Checkpointer: `AsyncPostgresSaver` when `DATABASE_URL` is configured, `InMemorySaver`
otherwise — generalized from single mode's hardcoded `InMemorySaver()`, now used by both
modes.** No live Postgres exists anywhere in this project yet (`docker/compose.yaml` is step
10) — a checkpointer that *required* Postgres would make every LangGraph run, single mode
included, impossible to execute at all right now. Same "not configured is a normal, expected
state" stance `api/health.py:check_postgres` already takes. `LangGraphRuntime._get_checkpointer`
builds this once per runtime instance and reuses it across every `run_async`/`resume` call
that instance makes — required for `InMemorySaver`, whose storage lives only in that one
Python object (a fresh one per call would make every interrupt permanently unresumable), and
kept for `AsyncPostgresSaver` too rather than reconnecting per call. Known limitation, not
worked around: `AsyncPostgresSaver`'s underlying `psycopg` connection is bound to the event
loop that created it, so a paused `run()` (sync, its own `asyncio.run()`-created loop) resumed
later via a separate `resume()` call (also sync, a *different* loop) would fail — only
`run_async`/`resume` called directly within one async caller's own loop (e.g. a future API
server) are guaranteed to share one. `InMemorySaver` has no such issue (plain dict-backed
storage, confirmed via its source, no event-loop-bound state at all), which is the only
checkpointer this PR's own tests exercise, since there is still no live Postgres to test the
other path against.

**Cost, threaded through per-role tokens rather than one model name.** Single mode computes
`cost_eur` once, from its one well-defined `investigator.model`. Multi mode has up to five
model calls (decompose, up to four `Send`-fanned workers, synthesize, critic) that could in
principle each use a different model — `AgentState`'s `cost_eur` field is a reducer, computed
node-by-node from that node's own `role_config["model"]` via `_compute_cost_eur`, and summed
the same way `tokens_in`/`tokens_out` already are. A `_BudgetExceeded` raised mid-run carries a
real `cost_eur` too (`langgraph_multi.py`'s `run_multi_async`/`resume_multi_async` catch and
re-raise it enriched, computed with the supervisor's model as a nominal stand-in for the
partial-breach report — every role currently shares one model in `config/roles.yaml`, and the
token counts themselves are the real, reducer-accumulated total regardless).

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
- Multi mode's supervisor prompt (`prompts/supervisor_langgraph.md`) and the SDK runtime's
  (`prompts/supervisor.md`) now have to be kept in substantive agreement by hand for the
  decompose/synthesize guidance they share — only the flagging section genuinely differs.
- A `resume()` after the process that ran `run_async` has exited only works when
  `DATABASE_URL` is configured (`InMemorySaver`'s state doesn't survive a process exit) — a
  real constraint on any future CLI/API wiring for resume, not just a test-time detail.
