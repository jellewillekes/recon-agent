---
paths:
  - "tests/**"
---

# Tests

- Mark anything that calls a model with `pytest.mark.llm`. CI runs `-m "not llm"`, so an unmarked model call breaks CI and spends credit.
- No network. Mock HTTP, the Agent SDK `query()` stream and MCP transports; see `tests/test_runtimes.py` and `tests/test_sec_edgar_fetch.py` for the patterns.
- Tool contract tests call the tool functions directly, with no LLM, and cover every `ToolResult` status in `docs/contracts.md` section 3.
- `tests/conftest.py` pins `RECON_TOOL_DATA=fixture`. Tests that need the EDGAR path build their own snapshot in `tmp_path` from a fictional payload.
- Test data is synthetic. Fixture companies are fictional; never copy a real company, filing or dataset question into a test.
- New behaviour starts with a failing test. Run it and show it fails before implementing.
- Never delete, skip or loosen a test to make it pass. If a test is wrong, say why and stop.
