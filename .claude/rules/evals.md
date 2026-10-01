---
paths:
  - "evals/**/*"
  - "config/rubrics/**/*"
  - "src/recon/eval/**/*"
---

# Evaluation harness

- Never edit a committed result in `evals/results/`. A new run is a new file.
- Never write, generate or change expected answers, golden cases or rubric assertions. Labels come from the dataset or the user. Rubric files marked "draft" stay draft until the user reviews them.
- Never lower a threshold in `config/thresholds.yaml` or loosen the gate to make a run pass. Its values are the user's.
- Two runs are only comparable with the same dataset (including its pin), rubric version, tool-data snapshot and set of cases. `gate.comparability_failures` enforces this (ADR 0018).
- Prompts, model config, runtime and mode are what a comparison is for, so they may differ. Say which of them differ when you compare two runs.
- With `--limit 3`, one case moves a mean by a third. Don't claim a trend from it.
- Report tokens and cost next to every quality number.
- For a second opinion on an eval result or a scoring change, use the `eval-reviewer` subagent.
