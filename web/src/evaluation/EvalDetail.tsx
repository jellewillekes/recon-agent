import { useEffect, useState } from "react";
import { api, type CaseScore, type EvalRun } from "../api/client";
import { failureCounts, FAILURE_LABELS, incompleteReasons, METRIC_LABELS, trajectorySummary } from "../lib/evals";
import { formatCost, formatDuration, number } from "../lib/format";

/** Keyed by `runId` where it's used, so a new run starts from "Loading". */
export function EvalDetail({ runId }: { runId: string }) {
  const [run, setRun] = useState<EvalRun | string | null>(null);

  useEffect(() => {
    let current = true;
    api.evalRun(runId).then(
      (loaded) => current && setRun(loaded),
      (error: Error) => current && setRun(error.message),
    );
    return () => {
      current = false;
    };
  }, [runId]);

  if (run === null) return <p className="no-data">Loading {runId}…</p>;
  if (typeof run === "string") return <div className="error-box">{run}</div>;
  return <RunDetail run={run} />;
}

export function RunDetail({ run }: { run: EvalRun }) {
  const aggregate = run.aggregate as Record<string, number | undefined>;
  const incomplete = incompleteReasons(aggregate);
  const failures = failureCounts(aggregate);
  return (
    <>
      <p className="compare-note">
        {run.run_id} · {run.runtime} / {run.mode} · routing {run.routing ? "on" : "off"} · rubric {run.rubric_version} ·{" "}
        {run.dataset}
      </p>
      {incomplete.length > 0 && (
        <div className="result-warning">
          <strong>Not every case was scored. </strong>
          The means include placeholder zeros, and the gate won't compare this run: {incomplete.join("; ")}.
        </div>
      )}
      <div className="metric-grid">
        {Object.entries(METRIC_LABELS).map(([name, label]) => {
          const value = name === "total_cost_eur" ? run.total_cost_eur : aggregate[name];
          if (value === undefined) return null;
          return (
            <div key={name} className="metric-tile">
              <span className="metric-label">{label}</span>
              <strong>{name === "elapsed_ms_mean" ? formatDuration(value) : number(value)}</strong>
            </div>
          );
        })}
      </div>
      {failures && (
        <p className="compare-note">
          Failure classes over {failures.classified} case(s): {failures.counts.map(([l, c]) => `${l} ${c}`).join(" · ")}
        </p>
      )}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              {["Case", "Completed", "Answer score", "Failure", "Run path", "Citation precision", "Cost", "Time", "Notes"].map(
                (h) => <th key={h}>{h}</th>,
              )}
            </tr>
          </thead>
          <tbody>
            {run.case_scores.map((score) => (
              <tr key={score.case_id} className={score.task_completion ? undefined : "failed-row"}>
                <td>{score.case_id.replace("finance-agent-bench:", "")}</td>
                <td>{score.task_completion ? "yes" : "no"}</td>
                <td>{score.judge_failed ? "unscored" : number(score.answer_score, 2)}</td>
                <td><FailureCell score={score} /></td>
                <td>{trajectorySummary(score.trajectory)}</td>
                <td>{number(score.citation_precision, 2)}</td>
                <td>{formatCost(score.cost_eur)}</td>
                <td>{formatDuration(score.elapsed_ms)}</td>
                <td>{score.notes}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function FailureCell({ score }: { score: CaseScore }) {
  if (!score.failure_class) return <>{score.failure_reason ?? "—"}</>;
  return (
    <span>
      <span className="failure" data-failure={score.failure_class}>
        {FAILURE_LABELS[score.failure_class] ?? score.failure_class}
      </span>
      {score.failure_reason && <small className="failure-reason">{score.failure_reason}</small>}
    </span>
  );
}
