# Contracts

The boundaries between modules. Change these deliberately, in their own PR, with the reason recorded.

Every schema is a Pydantic model in `src/recon/contracts.py`. No module reads or writes a boundary format directly.

---

## 1. External dataset -> internal

The public benchmark ships CSV with its own column layout. **Inspect the actual schema before writing the adapter** — the contract below describes our internal representation, not theirs.

Adapters live in `src/recon/adapters/`, one per source. An adapter may only map and validate. Never interpret, never fill in.

```python
class Case(BaseModel):
    case_id: str                          # stable, unique, from the source
    source: str                           # dataset name
    question: str
    expected_answer: str | None            # None = only the path is scorable
    expected_tool_path: list[str] | None   # tool names in expected order
    context: dict[str, Any]                # source documents, tickers, periods
    tags: list[str]                        # task category from the source
    license: str                           # e.g. "CC-BY-4.0"
    attribution: str                       # required attribution string
```

`license` and `attribution` are mandatory and are written into every evaluation result. Attribution obligations are then met automatically rather than by memory.

A case with neither `expected_answer` nor `expected_tool_path` is invalid and is rejected at load time.

---

## 2. Storage boundary

| Store | Used for | Not used for |
|---|---|---|
| DuckDB | Read-only analytical queries over Parquet and CSV, from tools | Any write path, any concurrent access |
| Postgres | Checkpoints, the review-flag table, pgvector embeddings | Columnar analytics over the dataset |

Tools query DuckDB. State goes to Postgres. A tool that needs to write is not a tool — it is a state operation and goes through the write path in section 6.

---

## 3. Tool contract

Every MCP tool returns this. Never a bare list, never `None`, never an exception reaching the agent.

```python
class ToolResult(BaseModel):
    status: Literal["ok", "empty", "truncated", "invalid_input", "unavailable"]
    data: list[dict[str, Any]]        # empty for anything but ok/truncated
    row_count: int
    message: str                       # always populated, including on ok
    elapsed_ms: int
```

**All five cases are mandatory and tested per tool:**

| status | When | `data` | `message` |
|---|---|---|---|
| `ok` | Result within limits | populated | what was retrieved |
| `empty` | Valid query, no result | `[]` | why it is empty, and what the caller could try |
| `truncated` | More rows than `MAX_ROWS` | first `MAX_ROWS` | how many exist, how to narrow |
| `invalid_input` | Schema or range error | `[]` | which field, and what is valid |
| `unavailable` | Source down, timeout, circuit open | `[]` | whether a retry is worthwhile |

`MAX_ROWS = 500` per call. `TIMEOUT_S = 30`.

`empty` is explicitly not an error. The agent must be able to conclude that nothing is there — for some cases that is the correct answer.

---

## 4. Agent result

```python
class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, Any]
    status: str                        # mirrors ToolResult.status
    elapsed_ms: int

class AgentResult(BaseModel):
    case_id: str
    answer: str
    evidence: list[str]                # references to tool output or source
    confidence: Literal["high", "medium", "low"]
    tool_calls: list[ToolCall]         # in call order
    runtime: str                       # which runtime produced this
    mode: Literal["single", "multi"]
    tokens_in: int
    tokens_out: int
    cost_eur: float
    elapsed_ms: int
    error: str | None
```

Empty `evidence` on a non-trivial answer is a signal, not an error — the critic and the rubric judge that.

Every runtime produces exactly this object. Runtimes are interchangeable as long as this contract holds.

---

## 5. API contract

```
POST /investigate
  body:     {question: str, context: dict, mode?: str, runtime?: str}
  200:      AgentResult
  400:      invalid input, with the offending field named
  422:      valid schema, unprocessable content
  504:      run exceeded the per-request timeout
  headers:  X-Request-ID echoed back on every response

GET /healthz   liveness  — process is up, no dependency checks
GET /readyz    readiness — MCP server reachable AND Postgres reachable
GET /metrics   Prometheus text format
```

`/healthz` must never check dependencies. A liveness probe that fails on a database blip restarts a healthy pod.

The request ID flows into the agent and appears on every span in the trace.

---

## 6. Write path

Exactly one write operation exists: `flag_case_for_review`.

```python
class ReviewFlag(BaseModel):
    idempotency_key: str               # unique constraint in Postgres
    case_id: str
    reason: str
    created_by: str                    # runtime + mode
    created_at: datetime
```

Rules:

- Requires an idempotency key. Calling twice with the same key produces one row
- Has a dry-run mode that returns the intended write without performing it
- Pauses the run for confirmation before executing
- Never called by a worker role. Supervisor only

---

## 7. Evaluation result

```python
class CaseScore(BaseModel):
    case_id: str
    task_completion: bool
    answer_score: float                # 0.0–1.0, from rubric
    tool_path_exact: bool
    tool_path_equivalent: bool         # different path, same evidence
    tool_call_accuracy: float          # 0.0–1.0
    rubric_scores: dict[str, float]    # per dimension
    cost_eur: float
    elapsed_ms: int
    notes: str

class EvalRun(BaseModel):
    run_id: str
    timestamp_utc: datetime
    dataset: str
    dataset_license: str
    dataset_attribution: str
    runtime: str
    mode: str
    model_config_hash: str
    prompt_hashes: dict[str, str]      # role -> hash of prompt file
    rubric_version: str
    case_scores: list[CaseScore]
    aggregate: dict[str, float]
    total_cost_eur: float
```

**`prompt_hashes` is not optional.** Without it a score is not reproducible and the promotion gate cannot work.

Write to `evals/results/<run_id>.json` plus a markdown summary. Commit both — this replaces running evaluations in CI.

---

## 8. Rubrics

Rubrics live in `config/rubrics/`, one YAML per dimension. Assertions, not free-form judgement.

```yaml
dimension: evidence_grounding
version: 1
weight: 0.3
assertions:
  - id: cites_tool_output
    text: "Every factual claim references retrieved tool output."
    score_if_true: 1.0
  - id: no_unsupported_numbers
    text: "No numbers appear in the answer that are absent from tool output."
    score_if_true: 1.0
```

Rubric changes are breaking: earlier runs are no longer comparable. Bump `rubric_version` and say so in the PR.

---

## 9. Promotion gate

A new prompt version or model configuration is rejected when:

- `task_completion` drops against the baseline, or
- weighted `answer_score` drops by more than 2%, or
- `total_cost_eur` rises by more than 20% without a rise in task completion

Baseline lives in `evals/baseline.json`. Replaced only through an explicit PR, never automatically.

---

## Change rules

- Changing a contract is its own PR, stating the reason and the effect on existing evaluation results
- Adding optional fields is fine; removing or renaming requires a migration
- `docs/contracts.md` and `src/recon/contracts.py` must agree. A test enforces that the model fields and this document do not drift apart
