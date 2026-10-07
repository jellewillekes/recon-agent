# 0029: Route multi mode's decompose step to a local model

## Context

Step 14 (#19) asks for a provider abstraction and for routing cheap work to a local
model, with the strong model keeping the final answer and the critic. The user runs this
project on the Claude subscription only, with no API key (ADR 0027).

Multi mode makes four kinds of call per case:

- decompose: routing the question to workers, as JSON
- workers: tool calls through MCP
- synthesis: the final answer
- critic: a check of that answer

Only decompose is a plain structured reply without tools.

## Decision

- `runtimes/providers.py` defines a `ModelProvider` protocol, one structured reply
  without tools, and `OllamaProvider`. The provider calls Ollama's REST API
  (`/api/chat` with a JSON-schema `format`) through httpx, which the project already
  depends on, so there's no new dependency. It sends no credential and costs nothing.
- `eval --routing on` sends multi mode's decompose step to the model in
  `config/models.yaml`'s `routing:` section. Workers, synthesis and the critic stay on
  Claude through the Agent SDK.
- The Agent SDK calls keep their own path, `_run_query`. They carry tools, the shared
  run budget and the partial-run handling, which a plain `complete()` doesn't model.
- Routing is recorded on `EvalRun` but isn't a comparability field. On against off is
  the comparison step 14 asks for, so the gate compares them like any change.
- `eval` refuses `--routing on` before spending credit when the runtime isn't the Agent
  SDK, the mode isn't multi, or Ollama isn't running with the model pulled.

## Consequences

- The local model's tokens aren't counted in the run's tokens or its token budget. They
  are free and use no Claude capacity, so on against off compares Claude tokens only.
- `recon.cli compare` shows two runs side by side with the gate's verdicts. That's the
  report of cost and task completion for both settings that step 14 asks for.
- The routed decompose can't call `flag_case_for_review`. The supervisor keeps it in
  synthesis.
- The saving is one short Claude call per case. What routing puts at risk is the quality
  of the decomposition, which the harness measures.
- LangGraph isn't routed. A local LangGraph model would need `langchain-ollama`, a new
  dependency, and reliable local tool calling.
- Step 14 mentions summarising tool output too. The workers do that inside their own
  tool-calling loop, so there's no separate step to route.
