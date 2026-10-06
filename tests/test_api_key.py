"""The metered API key for the LangGraph runtimes (ADR 0010).

It must reach `ChatAnthropic` and nothing else: the Agent SDK's CLI uses
`ANTHROPIC_API_KEY` whenever it is set, which would bill the SDK runtimes and
the judge to the API instead of the subscription.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from recon.contracts import Case
from recon.runtimes import agent_sdk, api_key, multi_agent
from recon.runtimes import langgraph as lg

pytestmark = pytest.mark.unit

CASE = Case(
    case_id="key-1",
    source="test",
    question="What was Fictional Corp's revenue?",
    expected_answer="n/a",
    expected_tool_path=None,
    context={},
    tags=[],
    license="MIT",
    attribution="test",
)


def test_the_langgraph_key_comes_from_its_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(api_key.KEY_ENV, "test-key")
    assert api_key.langgraph_api_key().get_secret_value() == "test-key"


def test_a_missing_langgraph_key_says_where_to_set_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(api_key.KEY_ENV, raising=False)
    with pytest.raises(RuntimeError, match=api_key.KEY_ENV):
        api_key.langgraph_api_key()


def test_langgraph_passes_the_key_to_the_model_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(api_key.KEY_ENV, "test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, connections: dict[str, Any]) -> None:
            captured["env"] = connections[lg.MCP_SERVER_NAME]["env"]

        async def get_tools(self) -> list[Any]:
            return []

    def chat_model(**kwargs: Any) -> Any:
        captured["api_key"] = kwargs.get("api_key")
        raise RuntimeError("stop before a model call")

    monkeypatch.setattr(lg, "MultiServerMCPClient", _Client)
    monkeypatch.setattr(lg, "ChatAnthropic", chat_model)

    asyncio.run(lg.LangGraphRuntime().run_async(CASE))

    assert captured["api_key"].get_secret_value() == "test-key"
    assert api_key.KEY_ENV not in captured["env"]


def test_no_tool_server_gets_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(api_key.KEY_ENV, "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sdk-key")
    single, _, _ = agent_sdk._build_options(
        Path("config/models.yaml"), Path("prompts/investigator.md")
    )
    roles = yaml.safe_load(Path("config/roles.yaml").read_text())
    facts = multi_agent._build_role_options(
        "worker_facts", roles["worker_facts"], Path("prompts"), {}
    )
    for options in (single, facts):
        servers: Any = options.mcp_servers
        env = servers[agent_sdk.MCP_SERVER_NAME]["env"]
        assert api_key.KEY_ENV not in env
        assert "ANTHROPIC_API_KEY" not in env


@pytest.mark.parametrize(
    ("runtime", "sdk_key", "langgraph_key", "refused"),
    [
        ("sdk", "set", None, True),
        ("langgraph", "set", "set", True),
        ("langgraph", None, None, True),
        ("langgraph", None, "set", False),
        ("sdk", None, None, False),
    ],
)
def test_eval_refuses_a_key_setup_that_would_bill_the_wrong_account(
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    sdk_key: str | None,
    langgraph_key: str | None,
    refused: bool,
) -> None:
    for name, value in (
        ("ANTHROPIC_API_KEY", sdk_key),
        (api_key.KEY_ENV, langgraph_key),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    problem = api_key.eval_key_problem(runtime)
    assert (problem is not None) is refused
