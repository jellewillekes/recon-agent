# 0032: The prompt check pins only the prompts the baseline read

## Context

`scripts/check_prompt_baseline.py` fails the commit and CI when any file in `prompts/` no
longer matches `evals/baseline.json`'s `prompt_hashes`. A baseline records the hash of
every prompt, but a run reads only some of them. The current baseline is an `agent_sdk`
single-mode run, which reads `investigator.md` and the faithfulness judge's prompt.
Changing `supervisor_langgraph.md` for #118 failed the check, although the baseline never
read that file. The only way through was a paid eval run to replace a baseline whose
scores couldn't have changed.

## Decision

Decided by the user on 2026-10-07:

- The check pins only the prompts the baseline's runtime and mode read
  (`hashing.prompts_read`). Single mode reads `investigator`. Multi mode reads one prompt
  per role in `config/roles.yaml`, with `supervisor_langgraph` in place of `supervisor`
  for LangGraph. Every run also reads `judge_faithfulness`.
- A change to any other prompt prints a notice and passes.
- A runtime the mapping doesn't know falls back to pinning every prompt.

## Consequences

- Prompts for runtimes or modes the baseline doesn't cover can change without a paid
  run. Their effect is measured only when someone evaluates that runtime or mode.
- The mapping has to follow the runtimes. A test checks that every prompt it names
  exists, so a renamed prompt fails there instead of silently leaving the check.
- The rubric judge's prompt lives in `eval/judge.py`, and the rubric version covers it.
  That's unchanged.
