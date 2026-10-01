---
paths:
  - "prompts/**/*"
  - "config/roles.yaml"
  - "config/models.yaml"
---

# Prompts, roles and models

- Prompts live in `prompts/`, one file per role, never inline in Python.
- Every prompt file is hashed into `EvalRun.prompt_hashes`, and `config/models.yaml` into `model_config_hash`. A change here makes the next eval incomparable with earlier ones unless you say so.
- After changing a prompt, a role's tool subset or a model, run `uv run python -m recon.cli eval --limit 3` only with the user's go-ahead (it spends credit), and report each metric before and after.
- Tool restrictions per role are enforced in `config/roles.yaml` when the MCP server is attached, not by prompt wording.
- Never paste dataset questions, or close paraphrases, into a prompt as examples. That measures recall of the eval set, not behaviour.
