import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, type Comparison, type EvalSummary } from "../api/client";
import { defaultComparePair, METRIC_LABELS } from "../lib/evals";
import { formatDuration, number } from "../lib/format";

function runLabel(run: EvalSummary): string {
  return `${run.run_id} · ${run.mode}${run.routing ? " · routing on" : ""} · rubric ${run.rubric_version}`;
}

function metricValue(name: string, value: number | null | undefined): string {
  if (value == null) return "—";
  return name === "elapsed_ms_mean" ? formatDuration(value) : number(value);
}

export function Compare({ runs }: { runs: EvalSummary[] }) {
  const [pair, setPair] = useState(() => defaultComparePair(runs));
  const [result, setResult] = useState<Comparison | string | null>(null);
  // Only the latest comparison may show: an earlier one finishing late would
  // otherwise put its result under the user's newer pair (review of #121).
  const latest = useRef(0);

  const compare = useCallback((baseline: string, candidate: string) => {
    const request = ++latest.current;
    api.compare(baseline, candidate).then(
      (comparison) => request === latest.current && setResult(comparison),
      (error: Error) => request === latest.current && setResult(error.message),
    );
  }, []);

  // Opens on the default pair, so the panel isn't empty on first view.
  useEffect(() => {
    if (runs.length < 2) return;
    const initial = defaultComparePair(runs);
    compare(initial.baseline, initial.candidate);
  }, [runs, compare]);

  // With one recorded run both sides start on it, and a run compared with
  // itself says nothing (review of #127).
  const samePair = pair.baseline === pair.candidate;

  function submit(event: FormEvent) {
    event.preventDefault();
    compare(pair.baseline, pair.candidate);
  }

  return (
    <>
      <form className="compare-form" onSubmit={submit}>
        <label>
          Baseline{" "}
          <select aria-label="Baseline run" value={pair.baseline} onChange={(e) => setPair({ ...pair, baseline: e.target.value })}>
            {runs.map((run) => <option key={run.run_id} value={run.run_id}>{runLabel(run)}</option>)}
          </select>
        </label>
        <label>
          Candidate{" "}
          <select aria-label="Candidate run" value={pair.candidate} onChange={(e) => setPair({ ...pair, candidate: e.target.value })}>
            {runs.map((run) => <option key={run.run_id} value={run.run_id}>{runLabel(run)}</option>)}
          </select>
        </label>
        <button type="submit" className="secondary-button" disabled={samePair}>Compare</button>
      </form>
      {samePair && <p className="compare-note">Pick two different runs to compare.</p>}
      <div id="compare-result">
        {typeof result === "string" && <div className="error-box">{result}</div>}
        {result && typeof result === "object" && <ComparisonTable result={result} />}
      </div>
    </>
  );
}

export function ComparisonTable({ result }: { result: Comparison }) {
  const short = (name: string, value: string) => (name === "model_config_hash" ? `${value.slice(0, 12)}…` : value);
  return (
    <div>
      {result.comparable ? (
        <p className="compare-note">Comparable. A change inside the measured run-to-run noise counts as "same".</p>
      ) : (
        <div className="result-warning">
          <strong>The gate won't compare these runs. </strong>
          They didn't measure the same thing, so the numbers below aren't evidence of better or worse.
          <ul className="reason-list">
            {result.reasons.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </div>
      )}
      <div className="table-scroll">
        <table className="data-table compare-table">
          <thead>
            <tr>
              <th></th>
              <th>{result.baseline}</th>
              <th>{result.candidate}</th>
              <th>Verdict</th>
              <th>Noise band</th>
            </tr>
          </thead>
          <tbody>
            {result.settings.map((setting) => (
              <tr key={setting.name} className="setting-row">
                <td>{setting.name}</td>
                <td>{short(setting.name, setting.baseline)}</td>
                <td>{short(setting.name, setting.candidate)}</td>
                <td></td>
                <td></td>
              </tr>
            ))}
            {result.metrics.map((metric) => (
              <tr key={metric.name}>
                <td>{METRIC_LABELS[metric.name] ?? metric.name}</td>
                <td>{metricValue(metric.name, metric.baseline)}</td>
                <td>{metricValue(metric.name, metric.candidate)}</td>
                <td>{metric.verdict && <span className="verdict" data-verdict={metric.verdict}>{metric.verdict}</span>}</td>
                <td>{metric.band ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
