---
paths:
  - "evals/**/*"
  - "config/rubrics/**/*"
  - "src/recon/eval/**/*"
---

# Evaluation harness

- Never edit a committed result in `evals/results/`. A new run is a new file.
- Never write, generate or change expected answers, golden cases or rubric assertions. Labels come from the dataset or the user. Rubric files marked "draft" stay draft until the user reviews them.
- Never lower a threshold or loosen the gate to make a run pass.
- Runs are only comparable with the same dataset, rubric version, prompt hashes, model config and tool-data snapshot. Say which of these differ when you compare two runs.
- With `--limit 3`, one case moves a mean by a third. Don't claim a trend from it.
- Report tokens and cost next to every quality number.
- For a second opinion on an eval result or a scoring change, use the `eval-reviewer` subagent.
