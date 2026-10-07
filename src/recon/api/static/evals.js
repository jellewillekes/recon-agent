// The evaluation view: eval runs (GET /evals), one run's cases
// (GET /evals/{id}) and the gate's comparison (GET /evals/compare).
// Benchmark results only; nothing here starts a run.
const evalTable = document.querySelector("#eval-table");
const evalDetail = document.querySelector("#eval-detail");
const compareForm = document.querySelector("#compare-form");
const compareBaseline = document.querySelector("#compare-baseline");
const compareCandidate = document.querySelector("#compare-candidate");
const compareResult = document.querySelector("#compare-result");
let evalsLoaded = false;

const METRIC_LABELS = {
  answer_score_mean: "Answer score",
  task_completion_rate: "Task completion",
  total_cost_eur: "Total cost (€)",
  cost_per_correct_answer_eur: "Cost per correct answer (€)",
  citation_precision_mean: "Citation precision",
  claim_support_rate_mean: "Key claims with verified source",
  tool_call_accuracy_mean: "Tool-call accuracy",
  elapsed_ms_mean: "Time per case",
};

// Failure classes (#116, ADR 0031), in the order they're checked.
const FAILURE_LABELS = {
  budget: "Budget",
  runtime_error: "Runtime error",
  tool_use: "Tool use",
  retrieval: "Retrieval",
  reasoning: "Reasoning",
  none: "Correct",
};

function number(value, digits = 3) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(digits);
}

function percent(value) {
  return value === null || value === undefined ? "—" : Math.round(value * 100) + "%";
}

function metricValue(name, value) {
  if (value === null || value === undefined) return "—";
  return name === "elapsed_ms_mean" ? formatDuration(value) : number(value);
}

function tableRow(cells, tag = "td") {
  const row = element("tr");
  for (const cell of cells) {
    const node = element(tag);
    if (cell instanceof Node) node.append(cell);
    else node.textContent = cell;
    row.append(node);
  }
  return row;
}

function runLabel(run) {
  return run.run_id + " · " + run.mode + (run.routing ? " · routing on" : "") + " · rubric " + run.rubric_version;
}

async function loadEvals() {
  if (evalsLoaded) return;
  try {
    const response = await fetch("/evals");
    const payload = await response.json();
    if (!response.ok) throw new Error(describeError(payload, "Couldn't load eval runs."));
    renderEvalTable(payload.runs);
    fillCompareSelects(payload.runs);
    evalsLoaded = true;
    if (payload.runs.length > 1) compareForm.requestSubmit();
  } catch (error) {
    evalTable.replaceChildren(tableRow([error.message]));
  }
}

function renderEvalTable(runs) {
  const head = element("thead");
  head.append(tableRow(["Run", "Recorded", "Mode", "Routing", "Rubric", "Cases", "Completion", "Answer score", "Cost", "€ / correct"], "th"));
  const body = element("tbody");
  for (const run of runs) {
    const open = element("button", "link-button", run.run_id);
    open.type = "button";
    open.addEventListener("click", () => loadEvalDetail(run.run_id));
    body.append(tableRow([
      open,
      new Date(run.timestamp_utc).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }),
      run.mode,
      run.routing ? "on" : "off",
      run.rubric_version,
      String(run.case_count),
      percent(run.task_completion_rate),
      number(run.answer_score_mean),
      formatCost(run.total_cost_eur),
      run.cost_per_correct_answer_eur == null ? "—" : formatCost(run.cost_per_correct_answer_eur),
    ]));
  }
  evalTable.replaceChildren(head, body);
}

// Defaults to the newest routing-off and routing-on multi runs, the
// comparison step 14 is measured by; otherwise the two newest runs.
function fillCompareSelects(runs) {
  const options = () => runs.map((run) => {
    const option = element("option", "", runLabel(run));
    option.value = run.run_id;
    return option;
  });
  compareBaseline.replaceChildren(...options());
  compareCandidate.replaceChildren(...options());
  const on = runs.find((r) => r.mode === "multi" && r.routing);
  const off = on && runs.find((r) => r.mode === "multi" && !r.routing && r.rubric_version === on.rubric_version);
  compareBaseline.value = off ? off.run_id : runs[1]?.run_id ?? runs[0]?.run_id ?? "";
  compareCandidate.value = on && off ? on.run_id : runs[0]?.run_id ?? "";
}

async function compareRuns(event) {
  event.preventDefault();
  const params = new URLSearchParams({ baseline: compareBaseline.value, candidate: compareCandidate.value });
  try {
    const response = await fetch("/evals/compare?" + params);
    const payload = await response.json();
    if (!response.ok) throw new Error(describeError(payload, "Couldn't compare those runs."));
    renderComparison(payload);
  } catch (error) {
    compareResult.replaceChildren(element("div", "error-box", error.message));
  }
}

function verdictBadge(verdict) {
  if (!verdict) return "";
  const badge = element("span", "verdict", verdict);
  badge.dataset.verdict = verdict;
  return badge;
}

function renderComparison(result) {
  const parts = [];
  if (result.comparable) {
    parts.push(element("p", "compare-note", "Comparable. A change inside the measured run-to-run noise counts as \"same\"."));
  } else {
    const box = element("div", "result-warning");
    box.append(element("strong", "", "The gate won't compare these runs. "));
    box.append(document.createTextNode("They didn't measure the same thing, so the numbers below aren't evidence of better or worse."));
    const list = element("ul", "reason-list");
    for (const reason of result.reasons) list.append(element("li", "", reason));
    box.append(list);
    parts.push(box);
  }
  const table = element("table", "data-table compare-table");
  const head = element("thead");
  head.append(tableRow(["", result.baseline, result.candidate, "Verdict", "Noise band"], "th"));
  const body = element("tbody");
  const short = (name, value) => (name === "model_config_hash" ? value.slice(0, 12) + "…" : value);
  for (const setting of result.settings) {
    const row = tableRow([setting.name, short(setting.name, setting.baseline), short(setting.name, setting.candidate), "", ""]);
    row.className = "setting-row";
    body.append(row);
  }
  for (const metric of result.metrics) {
    body.append(tableRow([
      METRIC_LABELS[metric.name] ?? metric.name,
      metricValue(metric.name, metric.baseline),
      metricValue(metric.name, metric.candidate),
      verdictBadge(metric.verdict),
      metric.band ?? "",
    ]));
  }
  table.append(head, body);
  const scroll = element("div", "table-scroll");
  scroll.append(table);
  parts.push(scroll);
  compareResult.replaceChildren(...parts);
}

function failureCell(score) {
  if (!score.failure_class) return score.failure_reason ?? "—";
  const cell = element("span");
  const badge = element("span", "failure", FAILURE_LABELS[score.failure_class] ?? score.failure_class);
  badge.dataset.failure = score.failure_class;
  cell.append(badge);
  if (score.failure_reason) cell.append(element("small", "failure-reason", score.failure_reason));
  return cell;
}

// The run path in one line: each step's score, "—" where it doesn't apply.
function trajectorySummary(path) {
  if (!path) return "—";
  const parts = [
    ["tools", path.tool_selection],
    ["args", path.argument_correctness],
    ["recovery", path.recovery],
    ["no repeats", path.efficiency],
    ["evidence", path.evidence_sufficiency],
  ];
  if (path.retrieval_quality) parts.push(["retrieval recall", path.retrieval_quality.recall]);
  return parts.map(([label, value]) => label + " " + number(value, 2)).join(" · ");
}

function renderFailureCounts(aggregate) {
  const classified = aggregate.failure_classified_cases;
  if (!classified) return null;
  const counts = Object.keys(FAILURE_LABELS)
    .filter((name) => aggregate["failure_" + name + "_count"])
    .map((name) => FAILURE_LABELS[name] + " " + aggregate["failure_" + name + "_count"]);
  return element("p", "compare-note", "Failure classes over " + classified + " case(s): " + counts.join(" · "));
}

async function loadEvalDetail(runId) {
  evalDetail.replaceChildren(element("p", "no-data", "Loading " + runId + "…"));
  try {
    const response = await fetch("/evals/" + encodeURIComponent(runId));
    const run = await response.json();
    if (!response.ok) throw new Error(describeError(run, "Couldn't load that run."));
    renderEvalDetail(run);
    evalDetail.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    evalDetail.replaceChildren(element("div", "error-box", error.message));
  }
}

function renderEvalDetail(run) {
  const summary = element("div", "metric-grid");
  for (const [name, label] of Object.entries(METRIC_LABELS)) {
    const value = name === "total_cost_eur" ? run.total_cost_eur : run.aggregate[name];
    if (value === undefined) continue;
    const tile = element("div", "metric-tile");
    tile.append(element("span", "metric-label", label), element("strong", "", metricValue(name, value)));
    summary.append(tile);
  }
  const meta = element(
    "p",
    "compare-note",
    run.run_id + " · " + run.runtime + " / " + run.mode + " · routing " + (run.routing ? "on" : "off") + " · rubric " + run.rubric_version + " · " + run.dataset,
  );
  const head = element("thead");
  head.append(tableRow(["Case", "Completed", "Answer score", "Failure", "Run path", "Citation precision", "Cost", "Time", "Notes"], "th"));
  const body = element("tbody");
  for (const score of run.case_scores) {
    const row = tableRow([
      score.case_id.replace("finance-agent-bench:", ""),
      score.task_completion ? "yes" : "no",
      score.judge_failed ? "unscored" : number(score.answer_score, 2),
      failureCell(score),
      trajectorySummary(score.trajectory),
      number(score.citation_precision, 2),
      formatCost(score.cost_eur),
      formatDuration(score.elapsed_ms),
      score.notes,
    ]);
    if (!score.task_completion) row.className = "failed-row";
    body.append(row);
  }
  const table = element("table", "data-table");
  table.append(head, body);
  const scroll = element("div", "table-scroll");
  scroll.append(table);
  const failures = renderFailureCounts(run.aggregate);
  evalDetail.replaceChildren(...[meta, summary, failures, scroll].filter(Boolean));
}

compareForm.addEventListener("submit", compareRuns);
