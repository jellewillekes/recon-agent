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

Tools query DuckDB, except `search_knowledge`, which reads the filing-text embeddings in pgvector (ADR 0025). Neither writes. State goes to Postgres. A tool that needs to write is not a tool — it is a state operation and goes through the write path in section 6.

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

Every row in `data` from the five read tools carries a `ref`: `"E"` plus the first 12 hex characters of the sha256 of the tool name, the company the call asked about (for the tools that take `company_id`) and the row's canonical JSON (ADR 0030). The same row always gets the same ref, so the model can cite it and the runtime can check the citation.

`MAX_ROWS = 500` per call. `TIMEOUT_S = 30`. `search_knowledge` returns at most `top_k` (up to 20) passages, so it never reports `truncated`.

`empty` is explicitly not an error. The agent must be able to conclude that nothing is there — for some cases that is the correct answer.

---

## 4. Agent result

```python
class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, Any]
    status: str                        # mirrors ToolResult.status
    elapsed_ms: int

class Evidence(BaseModel):
    ref: str                           # the row's ref, as the tool returned it
    verified: bool                     # ref matches a row a tool returned in this run
    source_type: Literal["filing_text", "financial_fact", "filing", "company", "concept", "unknown"]
    tool: str | None                   # the rest come from the row, None when unverified
    company_id: str | None
    form: str | None
    filed: str | None
    accession: str | None
    section: str | None
    locator: str | None                # chunk id, or concept and period for a fact
    excerpt: str                       # the passage or the fact, rendered from the row
    retrieval_score: float | None      # search_knowledge's rerank score
    content_hash: str | None           # sha256 of the row

class Claim(BaseModel):
    text: str
    importance: Literal["key", "supporting"]
    evidence_refs: list[str]           # Evidence.ref values

class AgentResult(BaseModel):
    case_id: str
    answer: str
    evidence: list[str]                # one line per cited source; filled from evidence_items when claims exist
    confidence: Literal["high", "medium", "low"]
    tool_calls: list[ToolCall]         # in call order
    runtime: str                       # which runtime produced this
    mode: Literal["single", "multi"]
    tokens_in: int
    tokens_out: int
    cost_eur: float
    elapsed_ms: int
    error: str | None
    claims: list[Claim]                # empty on older results, or when the model gave none
    evidence_items: list[Evidence]     # every ref the claims cite, resolved by the server
```

Claims and evidence are ADR 0030. Every row a read tool returns carries a `ref` (section 3). The model cites refs; the runtime resolves each one against the rows its tools returned in that run. A resolved ref is `verified`, and its source details and excerpt come from the row. A ref that matches nothing stays, unverified, so a made-up citation is visible instead of silently dropped.

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

GET /capabilities            runtimes and modes this deployment runs, the data source, whether runs are saved
GET /runs?limit=&offset=     saved runs, newest first (limit 1-100, default 20)
GET /runs/{run_id}           one saved run: question, settings, AgentResult, feedback
GET /runs/{run_id}/export    the same, as a JSON file to download
POST /runs/{run_id}/feedback body {rating: "up" | "down", note?: str}; replaces earlier feedback
  404:      no saved run with that id
  503:      run history is off: DATABASE_URL isn't set

GET /evals                   eval runs from evals/results/, newest first: settings, completion, score, cost
GET /evals/{run_id}          one EvalRun with its per-case scores
GET /evals/compare?baseline=&candidate=
                             settings and metrics side by side; the gate's verdict per gated
                             metric, or comparable: false with the reasons (ADR 0018). Also each run's 95%
                             interval per metric, the gate's noise rule, and warnings from stored repeat
                             runs (ADR 0036). None of these changes a verdict
  404:      no eval run with that id

POST /verify                 check an answer's claims against the tool rows they cite (ADR 0035)
  body:     {claims?: Claim[] (max 100), evidence?: row[] (max 1000)}
  200:      VerificationReport
  400:      invalid input, with the offending field named

GET /healthz   liveness  — process is up, no dependency checks
GET /readyz    readiness — MCP server reachable AND Postgres reachable
GET /metrics   Prometheus text format
```

`mode` is `single` or `multi`; `runtime` is `agent_sdk`. LangGraph is listed in `/capabilities` as unsupported: it needs a metered API key (ADR 0027).

Every answered `/investigate` is saved in Postgres (`research_runs`, ADR 0030) under its request ID, which is also `AgentResult.case_id`. A failed save is logged and the answer is still returned. No response carries environment values, keys or the database URL.

`/healthz` must never check dependencies. A liveness probe that fails on a database blip restarts a healthy pod.

`/verify` never calls a model and needs no Postgres. A claim that cites refs is checked against those rows only, and one that cites none against every row in `evidence`. A cited ref that matches no row leaves the claim UNSUPPORTED. Claims the numeric verifier can't read come back UNVERIFIABLE, and an answer with no claims is UNVERIFIABLE as a whole.

The `/evals` endpoints only read result files (`RECON_EVAL_RESULTS_DIR`, default `evals/results`). They never start a run. `recon.cli compare` prints the same comparison (`eval/comparison.py`).

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

class ReviewFlagResult(BaseModel):
    status: Literal["would_write", "confirmation_required", "created", "already_exists"]
    flag: ReviewFlag | None
    message: str
    preview_token: str | None
```

Rules:

- Requires an idempotency key. Calling twice with the same key produces one row.
  Enforced by a Postgres `UNIQUE` constraint on `idempotency_key`, via
  `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING *`, not just
  application-level de-duplication
- Has a dry-run mode (`dry_run=True`) that returns `status="would_write"` with
  the intended `ReviewFlag` and performs no write
- Pauses the run for confirmation before executing, and this is structurally
  required, not a prompt convention the caller could skip: calling without
  `confirmed=True` returns `status="confirmation_required"`, a `preview_token`
  deterministic in `case_id`/`reason`/`idempotency_key`/`created_by`, and
  performs no write. A `confirmed=True` call must present that exact
  `preview_token` or the write is refused — it cannot succeed as the first
  call for a given set of those four fields. A matching call performs the
  write, returning `status="created"` (or `"already_exists"` if that
  idempotency key was already written)
- Never called by a worker role. Supervisor only, enforced the same
  structural way as section 4's tool restriction — the tool is absent from a
  worker's `allowed_tools`, not merely refused

---

## 7. Evaluation result

```python
FailureClass = Literal["retrieval", "reasoning", "tool_use", "budget", "runtime_error", "none"]

class RetrievalQuality(BaseModel):     # the agent's own searches, replayed, against the case's relevance labels
    recall: float                      # labelled passages found among everything retrieved
    precision_at_5: float
    mrr_at_5: float
    ndcg_at_5: float
    retrieved_count: int

class TrajectoryScore(BaseModel):      # the run path scored step by step from the trace, no model call (#116); None = not applicable
    tool_selection: float | None = None        # overlap of the tools used with expected_tool_path; None without one
    argument_correctness: float | None = None  # calls the tool didn't reject as invalid_input
    retrieval_quality: RetrievalQuality | None = None  # only on labelled cases, with the filing-text search replayed
    evidence_sufficiency: float | None = None  # key claims with a verified ref (= claim_support_rate)
    recovery: float | None = None              # failed calls a later call to the same tool made good; None without failures
    efficiency: float | None = None            # calls that weren't an exact repeat of an earlier one
    grounding: float | None = None             # the evidence_grounding rubric score
    final_correctness: float | None = None     # the answer_correctness rubric score

class CaseScore(BaseModel):
    case_id: str
    task_completion: bool
    answer_score: float                # 0.0–1.0, weighted across rubric dimensions (§8 weight)
    tool_path_exact: bool
    tool_path_equivalent: bool         # different path, same evidence
    tool_call_accuracy: float          # 0.0–1.0
    rubric_scores: dict[str, float]    # per dimension
    cost_eur: float
    elapsed_ms: int
    notes: str
    tool_names: list[str] = []         # the agent's tool calls by name, in call order; empty before this field existed
    claim_support_rate: float | None   # key claims with a verified ref (ADR 0030); None when nothing to score
    citation_precision: float | None   # cited refs that are verified (ADR 0030); None when nothing is cited
    judge_failed: bool = False         # the rubric judge call failed, so answer_score and rubric_scores are placeholders (#123)
    faithfulness_judge_failed: bool = False  # the faithfulness judge call failed, so faithfulness is unscored (#123)
    trajectory: TrajectoryScore | None = None  # #116; None before this field existed
    failure_class: FailureClass | None = None  # the main reason the case failed, "none" when correct (ADR 0031); None when unscored
    failure_reason: str | None = None          # one line naming what the class rests on
    verifications: list[ClaimVerification] | None = None  # every claim of the answer, verified against its replayed fact calls (ADR 0035, 0038); [] when it made no claims; None when not verified, and before this field existed

class EvalRun(BaseModel):
    run_id: str
    timestamp_utc: datetime
    dataset: str                       # "<source>@<pinned commit>", e.g. finance-agent-bench@8ba65f81ab75
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
    tool_data_snapshot: str | None = None  # "<fetch date>-<content hash>" or "fixture-<hash>"; None before this field existed
    retrieval_labels_hash: str | None = None  # hash of evals/retrieval-labels.yaml when retrieval metrics were scored
    routing: bool = False              # decompose ran on the local model (step 14, ADR 0029); on vs off is gated like any change
    verifier_version: str | None = None  # "<rules>:tol=<tolerance>" the claims were verified with (ADR 0035); None when not verified
```

The gate doesn't use the retrieval metrics (`retrieval_*` in `aggregate`). Compare them by hand only between runs with the same `retrieval_labels_hash`, since a different label set changes them.

**`prompt_hashes` is not optional.** Without it a score is not reproducible and the promotion gate cannot work.

Write to `evals/results/<run_id>.json` plus a markdown summary. Commit both — this replaces running evaluations in CI.

Claims are verified with the numeric verifier (ADR 0034) and totalled into a `VerificationReport` (ADR 0035):

```python
Verdict = Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED", "CONTRADICTED", "STALE", "UNVERIFIABLE"]

class ClaimVerification(BaseModel):
    claim_id: str
    text: str                          # the claim as written
    verdict: Verdict
    evidence_refs: list[str]           # refs of the rows the verdict rests on
    claimed_value: float | None        # percentage points for growth and ratios, the row's unit for a level
    recomputed_value: float | None
    tolerance: float | None            # the largest difference still counted as a match
    reasoning: str

class VerificationReport(BaseModel):
    claims: list[ClaimVerification]
    counts: dict[str, int]             # claims per verdict, zero included
    grounding_score: float | None      # claims tied to at least one row, among all claims
    correctness_score: float | None    # SUPPORTED among the claims the verifier could read (not UNVERIFIABLE)
    freshness_score: float | None      # readable claims that are not STALE
    overall_verdict: Verdict           # the worst claim; UNVERIFIABLE with no claims
```

`overall_verdict` is CONTRADICTED, STALE or UNSUPPORTED if any claim has that verdict, in that order. Otherwise it is UNVERIFIABLE when no claim could be read, PARTIALLY_SUPPORTED when some could not, and SUPPORTED when all were.

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

Two runs are compared only when they share `rubric_version`, `dataset`, `tool_data_snapshot` and the same set of case ids. Otherwise the gate refuses before looking at any metric: the numbers measure different things. A baseline with no `tool_data_snapshot` is refused too. The fix is a new baseline through an explicit PR, or rerunning the candidate on the baseline's cases.

A comparable new prompt version or model configuration is rejected when:

- `task_completion` drops by more than one case, or
- weighted `answer_score` drops by more than 0.10 (absolute), or
- `total_cost_eur` rises by more than 20% without a rise in task completion

The first two allow for run-to-run noise, measured in `docs/eval-noise.md` (#77, ADR
0028). A change inside them is reported as "same". The limits live in
`config/thresholds.yaml`, with the correct-answer cutoff for
`cost_per_correct_answer_eur` and the minimums the baseline must reach.

When the baseline verified its claims (`verifier_version` is set), the gate also checks them (ADR 0035):

- the candidate must have verified its claims too, with the same `verifier_version`, or the runs aren't comparable
- the share of unsupported and contradicted claims, among those the verifier could read, rises by more than `claim_bad_rate_noise_band` (0 unless set in `config/thresholds.yaml`), or
- a case that completed in the baseline has more contradicted claims, whatever the share

A baseline that never verified claims is "not measured": the claim check is skipped, not passed.

Baseline lives in `evals/baseline.json`. Replaced only through an explicit PR, never automatically.

---

## Change rules

- Changing a contract is its own PR, stating the reason and the effect on existing evaluation results
- Adding optional fields is fine; removing or renaming requires a migration
- `docs/contracts.md` and `src/recon/contracts.py` must agree. A test enforces that the model fields and this document do not drift apart
