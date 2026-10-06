# 0027: The LangGraph API key lives in its own variable

## Context

The LangGraph runtimes call Claude through `ChatAnthropic`, billed to a metered API key
(ADR 0010). The Agent SDK runtimes and the judge, which every eval uses, bill the
subscription. The Agent SDK starts the `claude` CLI with this process's whole
environment. In non-interactive mode, the CLI always uses `ANTHROPIC_API_KEY` when it is
set, ahead of the subscription login (Claude Code's authentication docs). The SDK's
`options.env` can override a variable but can't remove one.

So exporting the key as `ANTHROPIC_API_KEY` for a LangGraph eval would quietly move the
judge's cost, and any SDK run's, to the API.

## Decision

- The LangGraph runtimes read the key from `RECON_ANTHROPIC_API_KEY` and pass it to
  `ChatAnthropic(api_key=...)`. Nothing reads it from `ANTHROPIC_API_KEY`
  (`runtimes/api_key.py`).
- `recon.cli eval` refuses to start when `ANTHROPIC_API_KEY` is set, for any runtime, and
  refuses a LangGraph eval without `RECON_ANTHROPIC_API_KEY`.
- No tool server, nor the health probe's, gets either key. They call no model.
- The key isn't kept in a file. The user keeps it in the macOS Keychain and sets it for
  one command (docs/runtimes.md).

## Consequences

- An exported `ANTHROPIC_API_KEY` blocks every eval until it is unset. That's the
  constraint ADR 0010 already stated for the SDK runtime, now checked.
- The variable name is project-specific, so tools that expect `ANTHROPIC_API_KEY` won't
  find the key. Only the LangGraph runtimes need it.
