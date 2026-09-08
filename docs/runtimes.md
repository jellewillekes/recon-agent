# Runtimes

Two runtimes implement `src/recon/runtimes/base.py::Runtime` (`run`/`run_async`), each
with a `single` and `multi` mode: `sdk×single`, `sdk×multi`, `langgraph×single`,
`langgraph×multi`. `docs/contracts.md` §4 is explicit that runtimes are interchangeable
as long as they produce a valid `AgentResult` — the harness (`src/recon/eval/harness.py`)
imports only the `Runtime` protocol, never a specific runtime module. This doc is a
comparison of how the two runtimes reach that same contract differently. The design
rationale behind each row lives in the ADRs; this doc summarizes and cross-references
rather than re-arguing them.

Select a combination with `--runtime {sdk,langgraph} --mode {single,multi}` on
`recon.cli run`/`eval`. `sdk`/`single` is the default.

## Primitives

| Dimension | `sdk` (`agent_sdk.py`, `multi_agent.py`) | `langgraph` (`langgraph.py`, `langgraph_multi.py`) |
|---|---|---|
| Orchestration | The Claude Agent SDK's own CLI-driven agentic loop (`claude_agent_sdk.query()`). Multi mode makes a separate `query()` call per role, orchestrated in our own Python | A hand-built LangGraph `StateGraph`. Single mode is `create_react_agent` (LangGraph's prebuilt ReAct graph). Multi mode adds `decompose` → `Send`-fanned worker subgraphs → `synthesize` → `critic` → `confirm_flag` |
| Model billing | The Agent SDK subscription credit | A separate, metered `ANTHROPIC_API_KEY` via `langchain_anthropic.ChatAnthropic` — `create_react_agent`/`StateGraph` need a real chat-model turn per step, which `query()`'s all-in-one loop has no way to expose |
| Tool restriction | `ClaudeAgentOptions.allowed_tools`, CLI-enforced per role. Critic gets no `mcp_servers` attached at all | `client.get_tools()`'s flat list filtered client-side against `config/roles.yaml` before binding to each role's model — same config, no per-connection scoping primitive on the MCP client itself |
| Checkpointing | None — a `query()` call runs to completion or fails, no mid-run persisted state | `AsyncPostgresSaver` when `DATABASE_URL` is set, `InMemorySaver` otherwise (both modes) |
| Human-in-the-loop interrupt | Multi mode only, via ordinary multi-turn tool-calling on `flag_case_for_review_tool` per `prompts/supervisor.md` — no dedicated pause primitive, the supervisor drives the whole dry-run/pause/confirm protocol itself | Multi mode only, via a dedicated `confirm_flag` graph node calling `interrupt()`, reached when the supervisor sets `flag_reason`. Resumed with `LangGraphRuntime.resume(case, thread_id, approved)` |
| Cost computation | `ResultMessage.total_cost_usd`, reported directly by the SDK | `config/models.yaml`'s static `pricing:` table applied to `ChatAnthropic`'s token counts — no live cost figure exists on this path |
| Budget enforcement fidelity | Live, per-tool-call, via a streaming `_BudgetTracker` | Single mode: live, per graph step, via `astream(stream_mode="values")`. Multi mode: only the *outer* graph's steps are checked live — a `Send`-fanned worker's own tool calls are invisible until its subgraph returns, bounded instead by `role_config["max_turns"]` as `recursion_limit` |
| Turn/step budget | `investigator.max_turns` / per-role `max_turns` (`config/models.yaml`, `config/roles.yaml`), enforced by the SDK CLI itself | Same config values, reused as `recursion_limit` — an approximation, not exact: roughly two graph steps per tool-call round trip in the prebuilt ReAct graph |

## Known gaps

- `langgraph×multi`'s worker-level budget check is coarser than every other
  combination's: a single stuck worker can spend up to its own `max_turns` before the
  run's tool-call budget gets a chance to stop it early. See
  `docs/adr/0010-langgraph-runtime.md`'s "Worker budget enforcement" section.
- `AsyncPostgresSaver`'s connection is bound to the event loop that created it — a
  `run()` and a later `resume()` from separate sync entry points are not guaranteed to
  share one. Only same-loop `run_async`/`resume` calls (e.g. a future API server) are
  safe. `InMemorySaver` has no such issue but doesn't survive a process exit.
- No CLI command wraps `LangGraphRuntime.resume()` yet — tests call it directly. A
  natural follow-up, not part of issue #14.
- `create_react_agent` is deprecated as of LangGraph 1.0 in favor of
  `langchain.agents.create_agent`, kept anyway for now (see ADR-0010's "Known, accepted
  gap").
- A paused `langgraph×multi` run's `AgentResult.tool_calls` excludes the
  `flag_case_for_review` calls until `resume()` completes — an interrupted node's
  return value only merges into graph state once the node finishes.

## Evaluation comparison

Not yet populated — see the "Runtimes" section of the README. Populating it means
running `recon.cli eval` for all four combinations, real spend against both the Agent
SDK credit and a configured `ANTHROPIC_API_KEY` (`CLAUDE.md`'s Cost section: never run
without being asked).

## See also

- `docs/adr/0007-multi-agent-orchestration.md` — why the SDK runtime uses separate
  `query()` calls per role instead of the SDK's own `agents=` subagent primitive.
- `docs/adr/0008-review-flag-write-path.md` — the write path both runtimes' confirm
  step calls into.
- `docs/adr/0009-tool-reliability-and-run-budgets.md` — retry/circuit-breaker semantics
  shared by the MCP tool layer both runtimes call.
- `docs/adr/0010-langgraph-runtime.md` — the full design record this table summarizes.
- `docs/contracts.md` §4 — the `AgentResult` contract both runtimes produce.
