import { describe, expect, it } from "vitest";
import {
  defaultComparePair,
  failureCounts,
  filterRuns,
  incompleteReasons,
  trajectorySummary,
} from "./evals";
import type { EvalSummary } from "../api/client";

function run(overrides: Partial<EvalSummary>): EvalSummary {
  return {
    run_id: "eval-a",
    timestamp_utc: "2026-10-07T12:00:00Z",
    runtime: "agent_sdk",
    mode: "multi",
    routing: false,
    rubric_version: "4",
    dataset: "finance-agent-bench@pin",
    case_count: 7,
    task_completion_rate: 1,
    answer_score_mean: 0.6,
    total_cost_eur: 1,
    cost_per_correct_answer_eur: 0.2,
    incomplete: [],
    ...overrides,
  };
}

describe("evaluation helpers", () => {
  it("names each kind of unmeasured case a run's aggregate records", () => {
    expect(incompleteReasons({ cases_judge_failed: 1, cases_skipped_at_cost_cap: 2 })).toEqual([
      "2 case(s) not run at the cost cap",
      "1 case(s) the judge couldn't score",
    ]);
    expect(incompleteReasons({ answer_score_mean: 0.5 })).toEqual([]);
  });

  it("counts failure classes in the order they're checked", () => {
    expect(
      failureCounts({ failure_classified_cases: 3, failure_none_count: 1, failure_tool_use_count: 2 }),
    ).toEqual({ classified: 3, counts: [["Tool use", 2], ["Correct", 1]] });
    expect(failureCounts({})).toBeNull();
  });

  it("summarizes a run path, with a dash where a step doesn't apply", () => {
    expect(
      trajectorySummary({
        argument_correctness: 1,
        efficiency: 0.5,
        retrieval_quality: { recall: 0.5, precision_at_5: 0.2, mrr_at_5: 1, ndcg_at_5: 0.6, retrieved_count: 4 },
      }),
    ).toBe("tools — · args 1.00 · recovery — · no repeats 0.50 · evidence — · retrieval recall 0.50");
    expect(trajectorySummary(null)).toBe("—");
  });

  it("defaults the comparison to routing off against routing on", () => {
    const runs = [
      run({ run_id: "on", routing: true }),
      run({ run_id: "single", mode: "single" }),
      run({ run_id: "off" }),
    ];
    expect(defaultComparePair(runs)).toEqual({ baseline: "off", candidate: "on" });
    expect(defaultComparePair([run({ run_id: "b" }), run({ run_id: "a" })])).toEqual({
      baseline: "a",
      candidate: "b",
    });
  });

  it("filters runs by mode, rubric and routing", () => {
    const runs = [run({ run_id: "1" }), run({ run_id: "2", mode: "single" }), run({ run_id: "3", rubric_version: "3" })];
    expect(filterRuns(runs, { mode: "multi", rubric: "", routing: "" }).map((r) => r.run_id)).toEqual(["1", "3"]);
    expect(filterRuns(runs, { mode: "", rubric: "3", routing: "" }).map((r) => r.run_id)).toEqual(["3"]);
    expect(filterRuns(runs, { mode: "", rubric: "", routing: "on" })).toEqual([]);
  });
});
