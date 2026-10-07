// What the evaluation view derives from eval runs: incomplete-run reasons,
// failure-class counts, run-path summaries, filters and the default compare.
import type { EvalSummary, TrajectoryScore } from "../api/client";
import { number } from "./format";

type Aggregate = Record<string, number | undefined>;

export const METRIC_LABELS: Record<string, string> = {
  answer_score_mean: "Answer score",
  task_completion_rate: "Task completion",
  total_cost_eur: "Total cost (€)",
  cost_per_correct_answer_eur: "Cost per correct answer (€)",
  citation_precision_mean: "Citation precision",
  claim_support_rate_mean: "Key claims with verified source",
  tool_call_accuracy_mean: "Tool-call accuracy",
  elapsed_ms_mean: "Time per case",
};

// Aggregate markers of cases a run didn't measure (#123), as in eval/gate.py.
const INCOMPLETE_KEYS: [string, string][] = [
  ["cases_skipped_at_cost_cap", "case(s) not run at the cost cap"],
  ["cases_skipped_at_session_limit", "case(s) cut short or not run at the session limit"],
  ["cases_judge_failed", "case(s) the judge couldn't score"],
];

// Failure classes (#116, ADR 0031), in the order they're checked.
export const FAILURE_LABELS: Record<string, string> = {
  budget: "Budget",
  runtime_error: "Runtime error",
  tool_use: "Tool use",
  retrieval: "Retrieval",
  reasoning: "Reasoning",
  none: "Correct",
};

export function incompleteReasons(aggregate: Aggregate): string[] {
  return INCOMPLETE_KEYS.filter(([key]) => aggregate[key]).map(
    ([key, text]) => `${aggregate[key]} ${text}`,
  );
}

export function failureCounts(
  aggregate: Aggregate,
): { classified: number; counts: [string, number][] } | null {
  const classified = aggregate.failure_classified_cases;
  if (!classified) return null;
  const counts = Object.entries(FAILURE_LABELS)
    .map(([name, label]): [string, number] => [label, aggregate[`failure_${name}_count`] ?? 0])
    .filter(([, count]) => count > 0);
  return { classified, counts };
}

/** The run path in one line: each step's score, "—" where it doesn't apply. */
export function trajectorySummary(path: Partial<TrajectoryScore> | null | undefined): string {
  if (!path) return "—";
  const parts: [string, number | null | undefined][] = [
    ["tools", path.tool_selection],
    ["args", path.argument_correctness],
    ["recovery", path.recovery],
    ["no repeats", path.efficiency],
    ["evidence", path.evidence_sufficiency],
  ];
  if (path.retrieval_quality) parts.push(["retrieval recall", path.retrieval_quality.recall]);
  return parts.map(([label, value]) => `${label} ${number(value, 2)}`).join(" · ");
}

/** The newest routing-off and routing-on multi runs on the same rubric, the
 * comparison step 14 is measured by; otherwise the two newest runs. */
export function defaultComparePair(runs: EvalSummary[]): { baseline: string; candidate: string } {
  const on = runs.find((r) => r.mode === "multi" && r.routing);
  const off = on && runs.find((r) => r.mode === "multi" && !r.routing && r.rubric_version === on.rubric_version);
  if (on && off) return { baseline: off.run_id, candidate: on.run_id };
  return {
    baseline: runs[1]?.run_id ?? runs[0]?.run_id ?? "",
    candidate: runs[0]?.run_id ?? "",
  };
}

export interface RunFilters {
  mode: string;
  rubric: string;
  routing: "" | "on" | "off";
}

export function filterRuns(runs: EvalSummary[], filters: RunFilters): EvalSummary[] {
  return runs.filter(
    (run) =>
      (!filters.mode || run.mode === filters.mode) &&
      (!filters.rubric || run.rubric_version === filters.rubric) &&
      (!filters.routing || run.routing === (filters.routing === "on")),
  );
}
