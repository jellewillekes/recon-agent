"""Every Agent SDK session this project opens is isolated from the machine.

Without isolation, the CLI loads the user's settings, CLAUDE.md, the skills
listing, and every claude.ai connector and user MCP server on the account,
adding ~110k tokens of foreign tool definitions to every turn and exposing
those tools to the agent. See docs/adr/0016-isolated-agent-sdk-sessions.md.
"""

from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from recon.eval import judge
from recon.runtimes import agent_sdk, multi_agent


def _assert_isolated(options: ClaudeAgentOptions) -> None:
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.skills == []


@pytest.mark.unit
def test_single_mode_investigator_is_isolated() -> None:
    options, _, _ = agent_sdk._build_options(
        agent_sdk.DEFAULT_MODELS_CONFIG_PATH, agent_sdk.DEFAULT_PROMPT_PATH
    )
    _assert_isolated(options)


@pytest.mark.unit
@pytest.mark.parametrize(
    "role", ["supervisor", "worker_lookup", "worker_facts", "critic"]
)
def test_every_multi_agent_role_is_isolated(role: str) -> None:
    roles = multi_agent._load_roles_config(Path("config/roles.yaml"))
    options = multi_agent._build_role_options(
        role, roles[role], Path("prompts"), multi_agent._WORKER_SCHEMA
    )
    _assert_isolated(options)


@pytest.mark.unit
def test_judge_is_isolated() -> None:
    judge_config, _ = judge._load_judge_config(judge.DEFAULT_MODELS_CONFIG_PATH)
    _assert_isolated(judge._build_options(judge_config))
