"""Tests for `runtimes/langgraph_multi.py` (issue #14 part 2: multi mode).

Same monkeypatching philosophy as `tests/test_langgraph_runtime.py`/
`tests/test_multi_agent.py`: fabricate every model call, no real Anthropic
API call, no cost. Workers go through the real local MCP server subprocess
(`tools/mcp_server.py`) via a fake tool-calling `ChatAnthropic` patched on
`langgraph.ChatAnthropic` (since `_build_react_subgraph` lives there and
workers reuse it unchanged from single mode); the decompose/synthesize/critic
structured calls use a separate fake patched on `langgraph_multi.ChatAnthropic`
- a distinct module-level import, patched separately.

The confirm-flag/interrupt tests reuse `tests/test_review_flag.py`'s own
`_FakeConnection`/`fake_postgres` pattern (patching `review_flag.asyncpg.connect`)
rather than a real Postgres - `flag_case_for_review`'s `dry_run`/unconfirmed
calls never touch it at all; only a `confirmed=True` write does.
"""

from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from recon.contracts import Case
from recon.runtimes import langgraph as lg
from recon.runtimes import langgraph_multi as lgm
from recon.tools import review_flag

pytestmark = pytest.mark.unit


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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

ROLES_CONFIG: dict[str, Any] = {
    "supervisor": {"model": "claude-sonnet-5", "max_turns": 8},
    "worker_lookup": {
        "model": "claude-sonnet-5",
        "max_turns": 8,
        "tools": ["list_companies", "list_financial_concepts"],
    },
    "worker_facts": {
        "model": "claude-sonnet-5",
        "max_turns": 8,
        "tools": ["get_financial_fact", "search_filings"],
    },
    "critic": {"model": "claude-sonnet-5", "max_turns": 3},
}

MODEL_CONFIG: dict[str, Any] = {
    "investigator": {"model": "claude-sonnet-5", "max_turns": 20},
    "pricing": {
        "claude-sonnet-5": {"input_usd_per_mtok": 2.00, "output_usd_per_mtok": 10.00}
    },
    "usd_to_eur_rate": 0.92,
    "run_budget": {
        "max_tool_calls": 30,
        "max_tokens": 300_000,
        "max_wall_clock_s": 100.0,
    },
}


def _run_multi(checkpointer: Any, **overrides: Any) -> Any:
    """`run_multi_async` with this file's generous defaults - individual
    tests override just the budget ceiling they're exercising, the same
    pattern `test_langgraph_runtime.py::_model_config` uses.
    """
    kwargs: dict[str, Any] = {
        "model_config": MODEL_CONFIG,
        "max_tool_calls": 30,
        "max_wall_clock_s": 100.0,
        "max_turns": 20,
        "roles_config_path": Path("unused"),
        "prompts_dir": Path("prompts"),
    }
    kwargs.update(overrides)
    return lgm.run_multi_async(CASE, checkpointer=checkpointer, **kwargs)


def _patch_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lgm, "_load_roles_config", lambda path: ROLES_CONFIG)


class _FakeStructuredOutput:
    """What `model.with_structured_output(schema)` returns for
    `create_react_agent`'s own final-answer step (worker subgraphs reuse
    this unchanged from single mode) - duplicated from
    `test_langgraph_runtime.py` rather than imported across test files,
    matching `test_multi_agent.py`'s independence from `test_runtimes.py`.
    """

    def __init__(self, model: "_FakeToolCallingModel", schema: type[BaseModel]) -> None:
        self._model = model
        self._schema = schema

    async def ainvoke(
        self, messages: Any, config: Any = None, **kwargs: Any
    ) -> BaseModel:
        response = self._model.responses[self._model._idx]
        self._model._idx += 1
        args = response.tool_calls[0]["args"]
        return self._schema(**args)


class _FakeToolCallingModel(BaseChatModel):
    """Backs worker subgraphs' tool-calling loop - same fake
    `test_langgraph_runtime.py` uses for single mode, duplicated here.
    """

    responses: list[AIMessage] = Field(default_factory=list)
    _idx: int = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeToolCallingModel":
        return self

    def with_structured_output(  # type: ignore[override]
        self, schema: type[BaseModel], **kwargs: Any
    ) -> _FakeStructuredOutput:
        return _FakeStructuredOutput(self, schema)

    def _generate(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self.responses[self._idx]
        self._idx += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"


def _patch_worker_model(
    monkeypatch: pytest.MonkeyPatch, responses: list[AIMessage]
) -> None:
    fake = _FakeToolCallingModel(responses=responses)
    monkeypatch.setattr(lg, "ChatAnthropic", lambda **kwargs: fake)


class _FakeStructuredCallModel:
    """Backs `langgraph_multi.ChatAnthropic` - decompose/synthesize/critic
    only ever call `.with_structured_output(schema, include_raw=True)
    .ainvoke(messages)` (`_structured_call`), never tool-calling. One shared
    instance across all three roles (they run sequentially, never in
    parallel - only workers are `Send`-fanned-out), so `responses` is
    consumed in call order: decompose, then synthesize, then critic.
    """

    def __init__(self, responses: list[BaseModel]) -> None:
        self._responses = responses
        self._idx = 0

    def with_structured_output(
        self, schema: type[BaseModel], *, include_raw: bool = False, **kwargs: Any
    ) -> "_FakeStructuredCallModel":
        assert include_raw is True
        return self

    async def ainvoke(
        self, messages: Any, config: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        response = self._responses[self._idx]
        self._idx += 1
        return {
            "raw": AIMessage(
                content="",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            ),
            "parsed": response,
            "parsing_error": None,
        }


def _patch_supervisor_model(
    monkeypatch: pytest.MonkeyPatch, responses: list[BaseModel]
) -> None:
    fake = _FakeStructuredCallModel(responses=responses)
    monkeypatch.setattr(lgm, "ChatAnthropic", lambda **kwargs: fake)


def _accepting_flow(flag_reason: str | None = None) -> list[BaseModel]:
    return [
        lgm.DecomposeResponse(
            subtasks=[
                lgm._Subtask(
                    worker="worker_lookup", instruction="find FIRM-001's sector"
                )
            ]
        ),
        lg.AnswerResponse(
            answer="Industrials",
            evidence=["FIRM-001 is in Industrials"],
            confidence="high",
            flag_reason=flag_reason,
        ),
        lgm.CriticResponse(accepted=True, reason="well supported"),
    ]


_ONE_WORKER_RESPONSES = [
    AIMessage(
        content="",
        tool_calls=[
            {
                "name": "list_companies_tool",
                "args": {"sector": "Industrials"},
                "id": "call1",
            }
        ],
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    ),
    # No tool_calls - this is what ends create_react_agent's main loop and
    # triggers its separate response_format step below (matches
    # test_langgraph_runtime.py's own single-mode fixtures exactly).
    AIMessage(content="Industrials."),
    AIMessage(
        content="",
        tool_calls=[
            {
                "name": "WorkerResponse",
                "args": {
                    "findings": "FIRM-001 is in Industrials",
                    "evidence": ["FIRM-001 sector=Industrials"],
                },
                "id": "r1",
            }
        ],
    ),
]


@pytest.mark.anyio
async def test_run_multi_success_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roles(monkeypatch)
    _patch_supervisor_model(monkeypatch, _accepting_flow())
    _patch_worker_model(monkeypatch, _ONE_WORKER_RESPONSES)

    outcome = await _run_multi(InMemorySaver())

    assert outcome.answer == "Industrials"
    assert outcome.evidence == ["FIRM-001 is in Industrials"]
    assert outcome.confidence == "high"
    assert len(outcome.tool_calls) == 1
    assert outcome.tool_calls[0].tool == "list_companies"
    # decompose(10,5) + worker(100,20) + synthesize(10,5) + critic(10,5).
    assert outcome.tokens_in == 130
    assert outcome.tokens_out == 35
    assert outcome.cost_eur > 0.0


@pytest.mark.anyio
async def test_run_multi_routes_to_both_workers_in_one_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the `Send`-based fan-out with two subtasks. Patches
    `_run_worker_task` directly, keyed by content rather than call order -
    each worker's own subgraph mechanics are already covered by
    `test_run_multi_success_full_flow`; this test's only concern is that
    decompose's two subtasks both reach a worker node and both contributions
    land in `AgentState`'s reducers, regardless of `Send`'s actual execution
    order (not guaranteed deterministic).
    """
    _patch_roles(monkeypatch)
    _patch_supervisor_model(
        monkeypatch,
        [
            lgm.DecomposeResponse(
                subtasks=[
                    lgm._Subtask(worker="worker_lookup", instruction="find FIRM-001"),
                    lgm._Subtask(worker="worker_facts", instruction="get its revenue"),
                ]
            ),
            lg.AnswerResponse(
                answer="Industrials, revenue reported",
                evidence=["FIRM-001 sector", "FIRM-001 revenue"],
                confidence="high",
            ),
            lgm.CriticResponse(accepted=True, reason="supported"),
        ],
    )
    seen_instructions: list[str] = []

    async def fake_run_worker_task(task: Any, **kwargs: Any) -> dict[str, Any]:
        seen_instructions.append(task["instruction"])
        return {
            "findings": [f"[{task['worker']}] found: {task['instruction']}"],
            "tool_calls": [],
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_eur": 0.0,
        }

    monkeypatch.setattr(lgm, "_run_worker_task", fake_run_worker_task)

    outcome = await _run_multi(InMemorySaver())

    assert sorted(seen_instructions) == ["find FIRM-001", "get its revenue"]
    assert outcome.answer == "Industrials, revenue reported"


@pytest.mark.anyio
async def test_critic_rejection_forces_confidence_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_roles(monkeypatch)
    _patch_supervisor_model(
        monkeypatch,
        [
            lgm.DecomposeResponse(
                subtasks=[
                    lgm._Subtask(worker="worker_lookup", instruction="look it up")
                ]
            ),
            lg.AnswerResponse(
                answer="Industrials", evidence=["a guess"], confidence="high"
            ),
            lgm.CriticResponse(
                accepted=False, reason="evidence doesn't back the claim"
            ),
        ],
    )
    _patch_worker_model(monkeypatch, _ONE_WORKER_RESPONSES)

    outcome = await _run_multi(InMemorySaver())

    assert outcome.confidence == "low"


class _FakeMcpTool:
    def __init__(self, name: str) -> None:
        self.name = name


_ALL_MCP_TOOL_NAMES = [
    "list_companies_tool",
    "list_financial_concepts_tool",
    "get_financial_fact_tool",
    "search_filings_tool",
    "flag_case_for_review_tool",
]


@pytest.mark.anyio
async def test_build_react_subgraph_restricts_worker_tools_to_role_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue's explicit ask: a worker must not be able to reach a tool
    outside `config/roles.yaml`'s subset for its role. `get_tools()` returns
    every MCP tool as one flat list with no per-connection scoping like the
    SDK's `allowed_tools` - `_build_react_subgraph`'s Python-side filter
    (ADR-0010) is what actually enforces this for `langgraph_multi.py`'s
    workers (`_run_worker_task` passes `tool_names=role_config["tools"]`).
    """

    class _FakeClient:
        def __init__(self, connections: Any) -> None:
            pass

        async def get_tools(self) -> list[_FakeMcpTool]:
            return [_FakeMcpTool(name) for name in _ALL_MCP_TOOL_NAMES]

    captured: dict[str, Any] = {}

    def fake_create_react_agent(model: Any, tools: Any, **kwargs: Any) -> str:
        captured["tools"] = tools
        return "fake-graph"

    monkeypatch.setattr(lg, "MultiServerMCPClient", _FakeClient)
    monkeypatch.setattr(lg, "create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(lg, "ChatAnthropic", lambda **kwargs: object())

    await lg._build_react_subgraph(
        model_name="claude-sonnet-5",
        prompt="prompt",
        response_format=lgm.WorkerResponse,
        created_by="langgraph:multi",
        tool_names=("list_companies", "list_financial_concepts"),
    )

    tool_names = {tool.name for tool in captured["tools"]}
    assert tool_names == {"list_companies_tool", "list_financial_concepts_tool"}
    assert "get_financial_fact_tool" not in tool_names
    assert "search_filings_tool" not in tool_names
    assert "flag_case_for_review_tool" not in tool_names


@pytest.mark.anyio
async def test_run_worker_task_passes_role_max_turns_as_recursion_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 review of this PR found `graph.ainvoke` here passing no
    `config` at all - `config/roles.yaml`'s per-role `max_turns` was silently
    never applied, LangGraph falling back to its own default recursion limit
    instead (see docs/adr/0010-langgraph-runtime.md's "Worker budget
    enforcement" paragraph for what this does and doesn't fix).
    """
    captured: dict[str, Any] = {}

    class _FakeGraph:
        async def ainvoke(self, input_state: Any, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            return {
                "structured_response": lgm.WorkerResponse(
                    findings="found it", evidence=["e1"]
                ),
                "messages": [],
            }

    async def fake_build_react_subgraph(**kwargs: Any) -> _FakeGraph:
        return _FakeGraph()

    monkeypatch.setattr(lgm, "_build_react_subgraph", fake_build_react_subgraph)

    await lgm._run_worker_task(
        lgm._WorkerTask(
            case_id=CASE.case_id, worker="worker_lookup", instruction="find it"
        ),
        roles_config=ROLES_CONFIG,
        model_config=MODEL_CONFIG,
        prompts_dir=Path("prompts"),
    )

    assert captured["config"] == {
        "recursion_limit": ROLES_CONFIG["worker_lookup"]["max_turns"]
    }


class _FakeReviewFlagConnection:
    """Mirrors `test_review_flag.py`'s own `_FakeConnection` - duplicated
    here rather than imported across test files (same independence
    `test_multi_agent.py` keeps from `test_runtimes.py`).
    """

    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    async def execute(self, sql: str, *params: Any) -> None:
        assert "CREATE TABLE" in sql

    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any] | None:
        if "INSERT INTO" in sql:
            key, case_id, reason, created_by, created_at = params
            if key in self._store:
                return None
            row = {
                "idempotency_key": key,
                "case_id": case_id,
                "reason": reason,
                "created_by": created_by,
                "created_at": created_at,
            }
            self._store[key] = row
            return row
        assert "SELECT" in sql
        (key,) = params
        return self._store.get(key)

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_postgres(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    store: dict[str, dict[str, Any]] = {}

    async def fake_connect(database_url: str) -> _FakeReviewFlagConnection:
        return _FakeReviewFlagConnection(store)

    monkeypatch.setattr(review_flag.asyncpg, "connect", fake_connect)
    return store


@pytest.mark.anyio
async def test_run_multi_pauses_when_supervisor_sets_flag_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dry_run`/unconfirmed `flag_case_for_review` calls never touch
    Postgres (proven here the same way `test_review_flag.py` proves it: by
    simply not patching `asyncpg.connect` - a stray write attempt would fail
    loudly, not silently pass) - only a `confirmed=True` resume does.

    `_confirm_flag_node`'s return value (its `tool_calls`) is only merged
    into `AgentState` once the node *completes* - it never does before an
    `interrupt()` call, so the paused state has no `flag_case_for_review`
    entries yet at all, not even the dry-run preview's. They all land at
    once, after `resume`, when the node's single successful pass finally
    returns (see `test_resume_approved_writes_the_flag`).
    """
    _patch_roles(monkeypatch)
    _patch_supervisor_model(
        monkeypatch, _accepting_flow(flag_reason="case looks mislabeled")
    )
    _patch_worker_model(monkeypatch, _ONE_WORKER_RESPONSES)

    with pytest.raises(lg._Paused) as exc_info:
        await _run_multi(InMemorySaver())

    assert exc_info.value.thread_id == CASE.case_id
    # decompose/workers/synthesize/critic already produced a real answer -
    # only the flag write is pending, so it isn't discarded.
    assert exc_info.value.outcome.answer == "Industrials"
    assert exc_info.value.outcome.confidence == "high"
    assert [
        call
        for call in exc_info.value.outcome.tool_calls
        if call.tool == "flag_case_for_review"
    ] == []


@pytest.mark.anyio
async def test_resume_approved_writes_the_flag(
    monkeypatch: pytest.MonkeyPatch, fake_postgres: dict[str, dict[str, Any]]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    _patch_roles(monkeypatch)
    _patch_supervisor_model(
        monkeypatch, _accepting_flow(flag_reason="case looks mislabeled")
    )
    _patch_worker_model(monkeypatch, _ONE_WORKER_RESPONSES)

    checkpointer = InMemorySaver()
    with pytest.raises(lg._Paused) as exc_info:
        await _run_multi(checkpointer)
    thread_id = exc_info.value.thread_id

    outcome = await lgm.resume_multi_async(
        CASE,
        thread_id,
        True,
        checkpointer=checkpointer,
        model_config=MODEL_CONFIG,
        max_tool_calls=30,
        max_wall_clock_s=100.0,
        max_turns=20,
        roles_config_path=Path("unused"),
        prompts_dir=Path("prompts"),
    )

    assert outcome.answer == "Industrials"
    assert len(fake_postgres) == 1
    flag_calls = [
        call for call in outcome.tool_calls if call.tool == "flag_case_for_review"
    ]
    assert [call.status for call in flag_calls] == [
        "would_write",
        "confirmation_required",
        "created",
    ]


@pytest.mark.anyio
async def test_resume_rejected_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roles(monkeypatch)
    _patch_supervisor_model(
        monkeypatch, _accepting_flow(flag_reason="case looks mislabeled")
    )
    _patch_worker_model(monkeypatch, _ONE_WORKER_RESPONSES)

    checkpointer = InMemorySaver()
    with pytest.raises(lg._Paused) as exc_info:
        await _run_multi(checkpointer)
    thread_id = exc_info.value.thread_id

    # Not patching asyncpg.connect at all - a stray write attempt on the
    # declined path would fail loudly instead of silently passing.
    outcome = await lgm.resume_multi_async(
        CASE,
        thread_id,
        False,
        checkpointer=checkpointer,
        model_config=MODEL_CONFIG,
        max_tool_calls=30,
        max_wall_clock_s=100.0,
        max_turns=20,
        roles_config_path=Path("unused"),
        prompts_dir=Path("prompts"),
    )

    assert outcome.answer == "Industrials"
    flag_calls = [
        call for call in outcome.tool_calls if call.tool == "flag_case_for_review"
    ]
    assert [call.status for call in flag_calls] == [
        "would_write",
        "confirmation_required",
    ]


@pytest.mark.anyio
async def test_run_multi_stops_early_on_tool_call_budget_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_budget` applies to the multi-mode graph at the same fidelity
    single mode has (part 1) - a worker's tool call counts against the same
    shared ceiling `_run_graph` already enforces mid-stream for single mode.
    """
    _patch_roles(monkeypatch)
    _patch_supervisor_model(monkeypatch, _accepting_flow())
    _patch_worker_model(monkeypatch, _ONE_WORKER_RESPONSES)

    with pytest.raises(lg._BudgetExceeded) as exc_info:
        await _run_multi(InMemorySaver(), max_tool_calls=1)

    assert "tool-call budget" in exc_info.value.reason
    assert len(exc_info.value.tool_calls) == 1
    assert exc_info.value.tool_calls[0].tool == "list_companies"
    # langgraph_multi.py's own re-raise (run_multi_async's except block)
    # computes a real cost here, unlike single mode's _BudgetExceeded, which
    # leaves this at 0.0 and lets LangGraphRuntime.run_async compute it
    # instead (no single model_name to hand it for multi mode).
    assert exc_info.value.cost_eur > 0.0
