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


def sdk_key_problem() -> str | None:
    """Why the Agent SDK would bill the API instead of the subscription, or None."""
    if os.environ.get(SDK_KEY_ENV):
        return (
            f"{SDK_KEY_ENV} is set, so the Agent SDK would bill the API instead of "
            f"the subscription. Unset it. A LangGraph key goes in {KEY_ENV}, for one "
            "command only (docs/runtimes.md)."
        )
    return None


def eval_key_problem(runtime: str) -> str | None:
    """Why a run with these keys would bill the wrong account, or None.

    Every eval judges on the Agent SDK, so `ANTHROPIC_API_KEY` must be unset
    for any runtime. A LangGraph run needs its own key.
    """
    problem = sdk_key_problem()
    if problem is not None:
        return problem
    if runtime == "langgraph" and not os.environ.get(KEY_ENV):
        return (
            f"--runtime langgraph needs {KEY_ENV}, the metered API key. Pass it "
            "for the one command (docs/runtimes.md)."
        )
    return None
