---
paths:
  - "src/recon/contracts.py"
  - "docs/contracts.md"
---

# Contracts

- `src/recon/contracts.py` and `docs/contracts.md` describe the same boundaries. Change them together, in the same PR.
- A contract change updates `tests/test_contracts.py` and every producer and consumer of the model.
- Prefer additive changes (a new optional field). Renaming or removing a field changes a module boundary, which needs the user's permission.
- A change to how scores are computed or recorded (`EvalRun`, `CaseResult`) needs a decision on `RUBRIC_VERSION` in `src/recon/eval/harness.py`, so old and new runs aren't compared as equals.
