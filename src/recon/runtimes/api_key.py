"""The metered API key the LangGraph runtimes call Claude with (ADR 0010).

It lives in `RECON_ANTHROPIC_API_KEY`, never `ANTHROPIC_API_KEY`. The Agent
SDK's CLI uses `ANTHROPIC_API_KEY` whenever it is set (non-interactive mode
always does), which would move the SDK runtimes' and the judge's cost from the
subscription to the API. The SDK can override a variable in the CLI's
environment but not remove one, so the key must never be exported under that
name.
"""

import os
from collections.abc import Mapping

from pydantic import SecretStr

KEY_ENV = "RECON_ANTHROPIC_API_KEY"
SDK_KEY_ENV = "ANTHROPIC_API_KEY"


def langgraph_api_key() -> SecretStr:
    """The key for `ChatAnthropic`, passed explicitly so nothing reads it from
    `ANTHROPIC_API_KEY`."""
    key = os.environ.get(KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{KEY_ENV} isn't set. The LangGraph runtime calls the metered API "
            "with it; see docs/runtimes.md for how to pass it for one command."
        )
    return SecretStr(key)


def without_api_keys(env: Mapping[str, str]) -> dict[str, str]:
    """`env` without either API key, for subprocesses that call no model."""
    return {k: v for k, v in env.items() if k not in (KEY_ENV, SDK_KEY_ENV)}


def eval_key_problem(runtime: str) -> str | None:
    """Why an eval with these keys would bill the wrong account, or None.

    Every eval judges on the Agent SDK, so `ANTHROPIC_API_KEY` must be unset
    for any runtime. A LangGraph eval needs its own key.
    """
    if os.environ.get(SDK_KEY_ENV):
        return (
            f"{SDK_KEY_ENV} is set, so the Agent SDK runs and the judge would bill "
            f"the API instead of the subscription. Unset it, and pass the LangGraph "
            f"key as {KEY_ENV} for the one command (docs/runtimes.md)."
        )
    if runtime == "langgraph" and not os.environ.get(KEY_ENV):
        return (
            f"--runtime langgraph needs {KEY_ENV}, the metered API key. Pass it "
            "for the one command (docs/runtimes.md)."
        )
    return None
