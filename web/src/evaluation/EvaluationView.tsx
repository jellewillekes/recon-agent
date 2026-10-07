import { useEffect, useRef, useState } from "react";
import { api, type EvalSummary } from "../api/client";
import { filterRuns, type RunFilters } from "../lib/evals";
import { formatCost, number, percent } from "../lib/format";
import { Compare } from "./Compare";
import { EvalDetail } from "./EvalDetail";

// The evaluation view: eval runs (GET /evals), the gate's comparison
// (GET /evals/compare) and one run's cases (GET /evals/{id}). Benchmark
// results only; nothing here starts a run.
export function EvaluationView({ active }: { active: boolean }) {
  const [runs, setRuns] = useState<EvalSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filters, setFilters] = useState<RunFilters>({ mode: "", rubric: "", routing: "" });
  const [detailId, setDetailId] = useState<string | null>(null);
  const detailRef = useRef<HTMLElement>(null);

  // Loads on first view. A failed load leaves `runs` empty, so showing the
  // view again retries, as the old page did.
  useEffect(() => {
    if (!active || runs !== null) return;
    api.evals().then(
      (list) => {
        setRuns(list.runs);
        setLoadError(null);
      },
      (error: Error) => setLoadError(error.message),
    );
  }, [active, runs]);

  function openDetail(runId: string) {
    setDetailId(runId);
    detailRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }

  const all = runs ?? [];
  const shown = filterRuns(all, filters);
  return (
    <>
      <section className="intro" aria-labelledby="eval-title">
        <div className="eyebrow eyebrow-eval"><span className="eyebrow-line"></span> Benchmark results · not live answers</div>
        <h1 id="eval-title">How we know it's good.<br /><span>Measured, not asserted.</span></h1>
        <p className="intro-copy">
          Each run scores the agent on the same fixed benchmark cases: task completion, answer score, cost, and whether
          its citations hold up. The gate only compares runs that measured the same thing.
        </p>
      </section>

      <section className="eval-card" aria-labelledby="eval-runs-heading">
        <div className="card-heading">
          <div>
            <span className="section-index">01 / EVALUATION RUNS</span>
            <h2 id="eval-runs-heading">Recorded runs</h2>
          </div>
        </div>
        <Filters runs={all} filters={filters} onChange={setFilters} />
        <div className="table-scroll">
          {loadError && runs === null ? (
            <p className="error-box">{loadError}</p>
          ) : (
            <RunTable runs={shown} loading={runs === null} onOpen={openDetail} />
          )}
        </div>
      </section>

      <section className="eval-card" aria-labelledby="compare-heading">
        <div className="card-heading">
          <div>
            <span className="section-index">02 / COMPARE</span>
            <h2 id="compare-heading">Baseline against candidate</h2>
          </div>
        </div>
        {all.length > 0 && <Compare runs={all} />}
      </section>

      <section className="eval-card" aria-labelledby="eval-detail-heading" ref={detailRef}>
        <div className="card-heading">
          <div>
            <span className="section-index">03 / RUN DETAIL</span>
            <h2 id="eval-detail-heading">Per-case results</h2>
          </div>
        </div>
        {detailId ? <EvalDetail key={detailId} runId={detailId} /> : <p className="no-data">Pick a run above to see its cases.</p>}
      </section>
    </>
  );
}

function Filters({
  runs,
  filters,
  onChange,
}: {
  runs: EvalSummary[];
  filters: RunFilters;
  onChange: (filters: RunFilters) => void;
}) {
  const modes = [...new Set(runs.map((r) => r.mode))].sort();
  const rubrics = [...new Set(runs.map((r) => r.rubric_version))].sort();
  return (
    <div className="run-filters" role="group" aria-label="Filter runs">
      <label>
        Mode
        <select aria-label="Filter by mode" value={filters.mode} onChange={(e) => onChange({ ...filters, mode: e.target.value })}>
          <option value="">all</option>
          {modes.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
      </label>
      <label>
        Rubric
        <select aria-label="Filter by rubric" value={filters.rubric} onChange={(e) => onChange({ ...filters, rubric: e.target.value })}>
          <option value="">all</option>
          {rubrics.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </label>
      <label>
        Routing
        <select
          aria-label="Filter by routing"
          value={filters.routing}
          onChange={(e) => onChange({ ...filters, routing: e.target.value as RunFilters["routing"] })}
        >
          <option value="">all</option>
          <option value="on">on</option>
          <option value="off">off</option>
        </select>
      </label>
    </div>
  );
}

function RunTable({
  runs,
  loading,
  onOpen,
}: {
  runs: EvalSummary[];
  loading: boolean;
  onOpen: (runId: string) => void;
}) {
  const headings = ["Run", "Recorded", "Mode", "Routing", "Rubric", "Cases", "Completion", "Answer score", "Cost", "€ / correct"];
  return (
    <table className="data-table">
      <thead>
        <tr>{headings.map((h) => <th key={h}>{h}</th>)}</tr>
      </thead>
      <tbody>
        {loading && <tr><td colSpan={headings.length}>Loading eval runs…</td></tr>}
        {!loading && runs.length === 0 && <tr><td colSpan={headings.length}>No runs match.</td></tr>}
        {runs.map((run) => (
          <tr key={run.run_id}>
            <td>
              <button className="link-button" type="button" onClick={() => onOpen(run.run_id)}>{run.run_id}</button>
            </td>
            <td>{new Date(run.timestamp_utc).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</td>
            <td>{run.mode}</td>
            <td>{run.routing ? "on" : "off"}</td>
            <td>{run.rubric_version}</td>
            <td>{run.case_count}</td>
            <td>{percent(run.task_completion_rate)}</td>
            <td>
              {number(run.answer_score_mean)}
              {run.incomplete?.length ? (
                <>
                  {" "}
                  {/* Its mean includes placeholder zeros (#123). */}
                  <span className="failure" data-failure="runtime_error" title={run.incomplete.join("; ")}>incomplete</span>
                </>
              ) : null}
            </td>
            <td>{formatCost(run.total_cost_eur)}</td>
            <td>{run.cost_per_correct_answer_eur == null ? "—" : formatCost(run.cost_per_correct_answer_eur)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
