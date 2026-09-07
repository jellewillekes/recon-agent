"""Production semantics from issue #13, part 2/3: retry with backoff and
jitter, a per-connection circuit breaker (`tools/server.py`), and per-run
budgets on tool calls, tokens, and wall-clock time (`runtimes/agent_sdk.py`,
`runtimes/multi_agent.py`).

Same fixture/monkeypatching philosophy as `tests/test_tools.py` and
`tests/test_runtimes.py`: a fake connection or a fake `query()` stream, no
real DuckDB failures, no LLM.
"""

import time
from typing import Any

import duckdb
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from recon.contracts import Case
from recon.runtimes import agent_sdk, multi_agent
from recon.tools import fixtures, server

CASE = Case(
    case_id="finance-agent-bench:abc123",
    source="finance-agent-bench",
    question="What sector is FIRM-001 in?",
    expected_answer="Industrials",
    expected_tool_path=None,
    context={
        "question_type": "Simple Lookups",
        "expert_time_minutes": 1.0,
        "rubric": {},
    },
    tags=["Simple Lookups"],
    license="MIT",
    attribution="test fixture",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- circuit breaker / retry (tools/server.py) ------------------------------


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    fixtures.seed(connection)
    return connection


class _FlakyConnection:
    """Wraps a real connection so `.cursor()` raises `duckdb.Error` for the
    first `fail_count` calls, then delegates normally. Each `.cursor()` call
    corresponds to one `_run_bounded_once` attempt, so this drives both the
    retry loop (a call whose failures don't exhaust `_MAX_ATTEMPTS`) and the
    circuit breaker (calls that do).
    """

    def __init__(self, real: duckdb.DuckDBPyConnection, fail_count: int) -> None:
        self._real = real
        self._fail_count = fail_count
        self.attempts = 0

    def cursor(self) -> duckdb.DuckDBPyConnection:
        self.attempts += 1
        if self.attempts <= self._fail_count:
            raise duckdb.Error(f"synthetic failure {self.attempts}")
        return self._real.cursor()


@pytest.mark.unit
@pytest.mark.parametrize("fail_count", [0, 1, 2])
def test_retry_recovers_within_max_attempts(
    conn: duckdb.DuckDBPyConnection, fail_count: int
) -> None:
    """Chaos: a call that fails 0, 1, or 2 times (fewer than _MAX_ATTEMPTS=3)
    still succeeds - the retry loop absorbs it.
    """
    flaky = _FlakyConnection(conn, fail_count=fail_count)

    result = server.list_companies(flaky, sector="Industrials")  # type: ignore[arg-type]

    assert result.status == "ok"
    assert flaky.attempts == fail_count + 1


@pytest.mark.unit
def test_retry_exhausted_returns_unavailable_not_the_breaker_message(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A call that fails on every attempt still ends as one `unavailable`
    result, distinct in wording from an open breaker's short-circuit.
    """
    flaky = _FlakyConnection(conn, fail_count=10)

    result = server.list_companies(flaky, sector="Industrials")  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert flaky.attempts == server._MAX_ATTEMPTS
    assert "circuit breaker" not in result.message


@pytest.mark.unit
def test_circuit_breaker_opens_after_three_consecutive_failing_calls(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    flaky = _FlakyConnection(conn, fail_count=10_000)  # always fails

    for _ in range(server._BREAKER_THRESHOLD):
        result = server.list_companies(flaky, sector="Industrials")  # type: ignore[arg-type]
        assert result.status == "unavailable"

    attempts_before_fourth_call = flaky.attempts
    fourth = server.list_companies(flaky, sector="Industrials")  # type: ignore[arg-type]

    assert fourth.status == "unavailable"
    assert "circuit breaker" in fourth.message
    # No new attempt was made - the breaker short-circuited before querying.
    assert flaky.attempts == attempts_before_fourth_call


@pytest.mark.unit
def test_circuit_breaker_closes_after_cooldown_and_a_successful_probe(
    conn: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_BREAKER_COOLDOWN_S", 0.01)
    # Each of the _BREAKER_THRESHOLD calls below retries up to _MAX_ATTEMPTS
    # times before giving up - enough failures to exhaust all of them, then
    # succeed on the cooldown probe.
    flaky = _FlakyConnection(
        conn, fail_count=server._BREAKER_THRESHOLD * server._MAX_ATTEMPTS
    )

    for _ in range(server._BREAKER_THRESHOLD):
        result = server.list_companies(flaky, sector="Industrials")  # type: ignore[arg-type]
        assert result.status == "unavailable"
    result = server.list_companies(flaky, sector="Industrials")  # type: ignore[arg-type]
    assert result.status == "unavailable"

    time.sleep(0.02)  # let the cooldown elapse
    # flaky's fail_count (== _BREAKER_THRESHOLD) is already exhausted, so this
    # probe call succeeds and should close the breaker.
    probe = server.list_companies(flaky, sector="Industrials")  # type: ignore[arg-type]
    assert probe.status == "ok"

    # A fresh single failure now doesn't reopen it - the breaker fully reset.
    flaky.attempts = 0
    flaky._fail_count = 1
    result = server.list_companies(flaky, sector="Industrials")  # type: ignore[arg-type]
    assert result.status == "ok"


class _SlowConnection:
    """Like `tests/test_tools.py`'s, but counts `.cursor()` calls so a
    retry (or the lack of one) is observable."""

    def __init__(self, real: duckdb.DuckDBPyConnection, delay_s: float) -> None:
        self._real = real
        self._delay_s = delay_s
        self.attempts = 0

    def cursor(self) -> "_SlowConnection":
        self.attempts += 1
        return self

    def execute(self, sql: str, params: list[object]) -> duckdb.DuckDBPyConnection:
        time.sleep(self._delay_s)
        return self._real.execute(sql, params)

    def interrupt(self) -> None:
        pass


@pytest.mark.unit
def test_timeout_is_not_retried(
    conn: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout counts as this call's failure immediately - unlike a plain
    `duckdb.Error`, it is never retried within `_MAX_ATTEMPTS`, since it
    already spent the full (here, monkeypatched-tiny) `TIMEOUT_S` once.
    """
    monkeypatch.setattr(server, "TIMEOUT_S", 0.01)
    slow = _SlowConnection(conn, delay_s=0.2)

    result = server.list_companies(slow, sector="Industrials")  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert slow.attempts == 1


@pytest.mark.unit
def test_circuit_breaker_opens_after_three_consecutive_timeouts_not_nine(
    conn: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before timeouts were exempted from retry, this took
    `_BREAKER_THRESHOLD * _MAX_ATTEMPTS` (9) attempts to open the breaker;
    now it takes exactly `_BREAKER_THRESHOLD` (3), one per call.
    """
    monkeypatch.setattr(server, "TIMEOUT_S", 0.01)
    slow = _SlowConnection(conn, delay_s=0.2)

    for _ in range(server._BREAKER_THRESHOLD):
        result = server.list_companies(slow, sector="Industrials")  # type: ignore[arg-type]
        assert result.status == "unavailable"

    assert slow.attempts == server._BREAKER_THRESHOLD

    fourth = server.list_companies(slow, sector="Industrials")  # type: ignore[arg-type]
    assert "circuit breaker" in fourth.message
    # No new attempt was made - the breaker short-circuited before querying.
    assert slow.attempts == server._BREAKER_THRESHOLD


# --- run budgets (runtimes/agent_sdk.py, runtimes/multi_agent.py) ----------


def _result_message(**overrides: Any) -> ResultMessage:
    defaults: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 100,
        "duration_api_ms": 80,
        "is_error": False,
        "num_turns": 1,
        "session_id": "session-1",
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "structured_output": {},
    }
    defaults.update(overrides)
    return ResultMessage(**defaults)


def _many_tool_call_messages(count: int) -> list[object]:
    messages: list[object] = []
    for i in range(count):
        tool_use_id = f"tu{i}"
        messages.append(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id=tool_use_id,
                        name="mcp__recon-tools__list_companies_tool",
                        input={"sector": None},
                    )
                ],
                model="claude-sonnet-5",
            )
        )
        messages.append(
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id=tool_use_id,
                        content='{"status": "ok", "elapsed_ms": 1}',
                    )
                ]
            )
        )
    return messages


@pytest.mark.unit
def test_run_stops_and_returns_partial_result_on_tool_call_budget_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_options(
        models_config_path: Any, prompt_path: Any
    ) -> tuple[ClaudeAgentOptions, float, agent_sdk.RunBudget]:
        return (
            ClaudeAgentOptions(),
            0.9,
            agent_sdk.RunBudget(
                max_tool_calls=1, max_tokens=10_000_000, max_wall_clock_s=3600.0
            ),
        )

    monkeypatch.setattr(agent_sdk, "_build_options", fake_build_options)

    async def fake_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> Any:
        for message in _many_tool_call_messages(5):
            yield message

    monkeypatch.setattr(agent_sdk, "query", fake_query)

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is not None
    assert "tool-call budget" in result.error
    # Stopped as soon as the budget (1) was reached, not after all 5.
    assert len(result.tool_calls) == 1
    assert result.confidence == "low"


@pytest.mark.unit
def test_wall_clock_breach_on_the_message_carrying_the_answer_keeps_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breach that would only be detected once the `ResultMessage` has
    already arrived must not discard the complete, valid answer it carries -
    unlike the tool-call budget test above, where no answer exists yet when
    the breach is detected mid-stream.
    """

    def fake_build_options(
        models_config_path: Any, prompt_path: Any
    ) -> tuple[ClaudeAgentOptions, float, agent_sdk.RunBudget]:
        return (
            ClaudeAgentOptions(),
            0.9,
            agent_sdk.RunBudget(
                max_tool_calls=1000, max_tokens=10_000_000, max_wall_clock_s=0.0
            ),
        )

    monkeypatch.setattr(agent_sdk, "_build_options", fake_build_options)

    async def fake_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> Any:
        yield _result_message(
            structured_output={
                "answer": "Industrials",
                "evidence": ["FIRM-001 sector fact"],
                "confidence": "high",
            }
        )

    monkeypatch.setattr(agent_sdk, "query", fake_query)

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert result.answer == "Industrials"
    assert result.confidence == "high"


@pytest.mark.unit
@pytest.mark.anyio
async def test_multi_mode_stops_the_run_on_token_budget_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token budget is checked between calls, not mid-stream - proven
    here by never issuing the worker call at all once the decompose call
    alone has already crossed it.
    """
    roles_config: dict[str, Any] = {
        "supervisor": {"model": "claude-sonnet-5", "max_turns": 4},
        "worker_lookup": {
            "model": "claude-sonnet-5",
            "max_turns": 8,
            "tools": ["list_companies"],
        },
        "worker_facts": {
            "model": "claude-sonnet-5",
            "max_turns": 8,
            "tools": ["get_financial_fact"],
        },
        "critic": {"model": "claude-sonnet-5", "max_turns": 3},
    }
    monkeypatch.setattr(multi_agent, "_load_roles_config", lambda path: roles_config)
    model_config = {
        "usd_to_eur_rate": 0.9,
        "run_budget": {
            "max_tool_calls": 1000,
            "max_tokens": 1,  # one decompose call's worth of tokens already breaches it
            "max_wall_clock_s": 3600.0,
        },
    }
    monkeypatch.setattr(multi_agent, "_load_model_config", lambda path: model_config)

    async def fake_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> Any:
        if prompt.startswith("Decompose"):
            yield _result_message(
                structured_output={
                    "subtasks": [
                        {"worker": "worker_lookup", "instruction": "find FIRM-001"}
                    ]
                }
            )
        else:
            raise AssertionError(
                f"should not have been called after the token budget breach: {prompt!r}"
            )

    monkeypatch.setattr(agent_sdk, "query", fake_query)

    result = await agent_sdk.AgentSdkRuntime(mode="multi").run_async(CASE)

    assert result.error is not None
    assert "token budget" in result.error
    assert result.confidence == "low"


@pytest.mark.unit
@pytest.mark.anyio
async def test_multi_mode_preserves_the_synthesized_answer_on_a_late_token_budget_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token budget breach detected right after the supervisor-synthesis
    call must not discard the answer that call already produced - unlike the
    decompose-call breach above, where no answer exists yet. The critic call
    is skipped (it would push further over budget), so the synthesized
    answer is returned as-is instead of an empty one.
    """
    roles_config: dict[str, Any] = {
        "supervisor": {"model": "claude-sonnet-5", "max_turns": 4},
        "worker_lookup": {
            "model": "claude-sonnet-5",
            "max_turns": 8,
            "tools": ["list_companies"],
        },
        "worker_facts": {
            "model": "claude-sonnet-5",
            "max_turns": 8,
            "tools": ["get_financial_fact"],
        },
        "critic": {"model": "claude-sonnet-5", "max_turns": 3},
    }
    monkeypatch.setattr(multi_agent, "_load_roles_config", lambda path: roles_config)
    model_config = {
        "usd_to_eur_rate": 0.9,
        "run_budget": {
            "max_tool_calls": 1000,
            # decompose (15) + worker (15) fits; + synthesis (15) doesn't.
            "max_tokens": 40,
            "max_wall_clock_s": 3600.0,
        },
    }
    monkeypatch.setattr(multi_agent, "_load_model_config", lambda path: model_config)

    async def fake_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> Any:
        if prompt.startswith("Decompose"):
            yield _result_message(
                structured_output={
                    "subtasks": [
                        {"worker": "worker_lookup", "instruction": "find FIRM-001"}
                    ]
                }
            )
        elif prompt == "find FIRM-001":
            yield _result_message(
                structured_output={
                    "findings": "FIRM-001 is in Industrials",
                    "evidence": ["fact:sector=Industrials"],
                }
            )
        elif prompt.startswith("Original question"):
            yield _result_message(
                structured_output={
                    "answer": "Industrials",
                    "evidence": ["fact:sector=Industrials"],
                    "confidence": "high",
                }
            )
        else:
            raise AssertionError(
                f"critic should not have been called after the token budget "
                f"breach: {prompt!r}"
            )

    monkeypatch.setattr(agent_sdk, "query", fake_query)

    result = await agent_sdk.AgentSdkRuntime(mode="multi").run_async(CASE)

    assert result.error is not None
    assert "token budget" in result.error
    assert result.answer == "Industrials"
    assert result.evidence == ["fact:sector=Industrials"]
    assert result.confidence == "high"
