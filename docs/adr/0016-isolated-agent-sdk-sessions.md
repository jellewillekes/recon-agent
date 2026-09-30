# ADR 0016: Isolate every Agent SDK session from the machine it runs on

Status: Accepted
Date: 2026-09-28
Checked: 2026-09-30. `setting_sources`, `strict_mcp_config` and `skills` still exist on
`ClaudeAgentOptions` in claude-agent-sdk 0.2.150 (`tests/test_sdk_isolation.py`). The token
figures below were measured on 2026-09-28 and not re-measured.

## Context

Three single-mode runs on real EDGAR data each used 500k–720k input tokens for only 3–7
tool calls, and every one breached the 300k run budget (#66). Measuring one minimal turn
("reply OK") showed why. With the options as built, the session loaded:

- our MCP server, plus every claude.ai connector on the account (mail, notes, music).
  That's 89 tool definitions, 83 of them foreign.
- the user, project and local settings files, including the repo's `CLAUDE.md`
- the CLI's skills listing

That came to 116,203 context tokens per turn. Our own system prompt and six tools are
4,714. The SDK's defaults explain it. `setting_sources=None` loads every settings
source, like the CLI does. Claude.ai connectors and user MCP servers load unless
`strict_mcp_config=True`. `skills=None` keeps the CLI's own skills listing. `tools` and
`allowed_tools` don't help: `tools` covers built-in tools only, and `allowed_tools`
controls permission, not which definitions are sent.

It is a safety problem as well as a cost one. The foreign tools were refused by
`allowed_tools`, but the agent could see them, including ones that send mail or write
pages. That's more surface for a prompt injection in tool output to aim at (step 8's
threat model). It also made runs depend on whose machine they ran on.

## Decision

`agent_sdk.ISOLATED_SESSION` sets `setting_sources=[]`, `strict_mcp_config=True` and
`skills=[]`. It is applied to every `ClaudeAgentOptions` the project builds: the
single-mode investigator, every multi-agent role, and the LLM judge. A test asserts all
of them. The LangGraph runtime calls the API directly, not through the CLI, so it isn't
affected.

Isolation doesn't lose anything the agent should have. It needs only its own system
prompt file, our MCP server, and `Read`, all of which are passed explicitly.

## Consequences

- Per-turn context drops about 96% (116k → 4.7k tokens measured), and so does most of
  the per-case cost. The first measured turn cost $0.47 before and $0.02 after.
- Runs and eval results no longer depend on the machine's settings, connectors or
  skills, so two machines run the same agent.
- The token budget in `config/models.yaml` (#66) was breached by overhead, not by the
  agent's work. Whether it still needs changing should be judged on isolated runs.
- If a future feature needs a skill, a setting or another MCP server, it has to be
  passed explicitly. It can't be picked up from the environment.
