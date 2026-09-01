"""Tests for src/recon/contracts.py.

Covers: every model's required fields are actually required, the Case
answer-or-path invariant, every ToolResult status/data combination from
docs/contracts.md section 3, and a drift check between docs/contracts.md and
the models themselves.
"""

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError

from recon.contracts import (
    AgentResult,
    Case,
    CaseScore,
    EvalRun,
    ReviewFlag,
    ToolCall,
    ToolResult,
)

CONTRACTS_MD = Path(__file__).parent.parent / "docs" / "contracts.md"
ToolResultStatus = Literal["ok", "empty", "truncated", "invalid_input", "unavailable"]

VALID_CASE: dict[str, Any] = {
    "case_id": "case-001",
    "source": "finance_agent_bench",
    "question": "What was the reported figure for the period?",
    "expected_answer": "42",
    "expected_tool_path": None,
    "context": {"ticker": "unused"},
    "tags": ["valuation"],
    "license": "CC-BY-4.0",
    "attribution": "Source dataset, required attribution text.",
}

VALID_TOOL_RESULT_OK: dict[str, Any] = {
    "status": "ok",
    "data": [{"row": 1}],
    "row_count": 1,
    "message": "found 1 row",
    "elapsed_ms": 5,
}

VALID_TOOL_CALL: dict[str, Any] = {
    "tool": "query_ledger",
    "arguments": {"query": "select 1"},
    "status": "ok",
    "elapsed_ms": 5,
}

VALID_AGENT_RESULT: dict[str, Any] = {
    "case_id": "case-001",
    "answer": "42",
    "evidence": ["row 1"],
    "confidence": "high",
    "tool_calls": [ToolCall(**VALID_TOOL_CALL)],
    "runtime": "agent_sdk",
    "mode": "single",
    "tokens_in": 100,
    "tokens_out": 20,
    "cost_eur": 0.01,
    "elapsed_ms": 500,
    "error": None,
}

VALID_REVIEW_FLAG: dict[str, Any] = {
    "idempotency_key": "key-001",
    "case_id": "case-001",
    "reason": "figure disagrees with source document",
    "created_by": "agent_sdk:single",
    "created_at": datetime.now(UTC),
}

VALID_CASE_SCORE: dict[str, Any] = {
    "case_id": "case-001",
    "task_completion": True,
    "answer_score": 0.9,
    "tool_path_exact": True,
    "tool_path_equivalent": False,
    "tool_call_accuracy": 0.8,
    "rubric_scores": {"answer_correctness": 0.9},
    "cost_eur": 0.01,
    "elapsed_ms": 500,
    "notes": "matches expected answer",
}

VALID_EVAL_RUN: dict[str, Any] = {
    "run_id": "run-001",
    "timestamp_utc": datetime.now(UTC),
    "dataset": "finance_agent_bench",
    "dataset_license": "CC-BY-4.0",
    "dataset_attribution": "Source dataset, required attribution text.",
    "runtime": "agent_sdk",
    "mode": "single",
    "model_config_hash": "deadbeef",
    "prompt_hashes": {"investigator": "cafef00d"},
    "rubric_version": "1",
    "case_scores": [CaseScore(**VALID_CASE_SCORE)],
    "aggregate": {"task_completion": 1.0},
    "total_cost_eur": 0.01,
}

MODEL_CASES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (Case, VALID_CASE),
    (ToolResult, VALID_TOOL_RESULT_OK),
    (ToolCall, VALID_TOOL_CALL),
    (AgentResult, VALID_AGENT_RESULT),
    (ReviewFlag, VALID_REVIEW_FLAG),
    (CaseScore, VALID_CASE_SCORE),
    (EvalRun, VALID_EVAL_RUN),
]


@pytest.mark.parametrize(
    "model_cls, valid_kwargs", MODEL_CASES, ids=[c.__name__ for c, _ in MODEL_CASES]
)
def test_valid_kwargs_construct(
    model_cls: type[BaseModel], valid_kwargs: dict[str, Any]
) -> None:
    """The fixtures above are themselves valid, so the missing-field tests are meaningful."""
    model_cls(**valid_kwargs)


MISSING_FIELD_CASES: list[tuple[type[BaseModel], dict[str, Any], str]] = [
    (model_cls, valid_kwargs, field_name)
    for model_cls, valid_kwargs in MODEL_CASES
    for field_name in valid_kwargs
]


@pytest.mark.parametrize(
    "model_cls, valid_kwargs, field_name",
    MISSING_FIELD_CASES,
    ids=[f"{c.__name__}.{f}" for c, _, f in MISSING_FIELD_CASES],
)
def test_required_field_missing_raises(
    model_cls: type[BaseModel], valid_kwargs: dict[str, Any], field_name: str
) -> None:
    kwargs = {k: v for k, v in valid_kwargs.items() if k != field_name}
    with pytest.raises(ValidationError):
        model_cls(**kwargs)


def test_case_rejects_neither_answer_nor_tool_path() -> None:
    kwargs = {**VALID_CASE, "expected_answer": None, "expected_tool_path": None}
    with pytest.raises(ValidationError):
        Case(**kwargs)


def test_case_accepts_tool_path_only() -> None:
    kwargs = {
        **VALID_CASE,
        "expected_answer": None,
        "expected_tool_path": ["query_ledger"],
    }
    Case(**kwargs)


def test_case_accepts_answer_only() -> None:
    kwargs = {**VALID_CASE, "expected_answer": "42", "expected_tool_path": None}
    Case(**kwargs)


@pytest.mark.parametrize(
    "status, data, row_count",
    [
        ("ok", [{"row": 1}], 1),
        ("truncated", [{"row": i} for i in range(3)], 500),
        ("empty", [], 0),
        ("invalid_input", [], 0),
        ("unavailable", [], 0),
    ],
)
def test_tool_result_valid_status_data_combinations(
    status: ToolResultStatus, data: list[dict[str, Any]], row_count: int
) -> None:
    ToolResult(
        status=status, data=data, row_count=row_count, message="ok", elapsed_ms=1
    )


@pytest.mark.parametrize(
    "status, data, row_count",
    [
        ("ok", [], 0),
        ("empty", [{"row": 1}], 1),
        ("truncated", [], 0),
        ("truncated", [{"row": 1}], 1),
        ("invalid_input", [{"row": 1}], 1),
        ("unavailable", [{"row": 1}], 1),
    ],
)
def test_tool_result_invalid_status_data_combinations(
    status: ToolResultStatus, data: list[dict[str, Any]], row_count: int
) -> None:
    with pytest.raises(ValidationError):
        ToolResult(
            status=status, data=data, row_count=row_count, message="ok", elapsed_ms=1
        )


def test_tool_result_message_required_even_on_ok() -> None:
    with pytest.raises(ValidationError):
        ToolResult(
            status="ok", data=[{"row": 1}], row_count=1, message="", elapsed_ms=1
        )


def test_case_score_answer_score_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        CaseScore(**{**VALID_CASE_SCORE, "answer_score": 1.5})


def test_case_score_tool_call_accuracy_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        CaseScore(**{**VALID_CASE_SCORE, "tool_call_accuracy": -0.1})


def test_eval_run_rejects_empty_prompt_hashes() -> None:
    with pytest.raises(ValidationError):
        EvalRun(**{**VALID_EVAL_RUN, "prompt_hashes": {}})


def _documented_model_fields() -> dict[str, set[str]]:
    """Extract {class_name: {field_names}} from the python code fences in docs/contracts.md."""
    text = CONTRACTS_MD.read_text()
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    fields: dict[str, set[str]] = {}
    for block in blocks:
        tree = ast.parse(block)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                fields[node.name] = {
                    stmt.target.id
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                }
    return fields


DOCUMENTED_MODELS: dict[str, type[BaseModel]] = {
    "Case": Case,
    "ToolResult": ToolResult,
    "ToolCall": ToolCall,
    "AgentResult": AgentResult,
    "ReviewFlag": ReviewFlag,
    "CaseScore": CaseScore,
    "EvalRun": EvalRun,
}


def test_docs_contracts_fields_match_models() -> None:
    """docs/contracts.md and contracts.py must not drift apart (docs/contracts.md, Change rules)."""
    documented = _documented_model_fields()
    assert set(documented) == set(DOCUMENTED_MODELS)
    for name, model_cls in DOCUMENTED_MODELS.items():
        actual_fields = set(model_cls.model_fields)
        assert actual_fields == documented[name], (
            f"{name}: docs/contracts.md fields {sorted(documented[name])} != "
            f"contracts.py fields {sorted(actual_fields)}"
        )
