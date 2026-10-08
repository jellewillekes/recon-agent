import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type AgentResult, type Capabilities, type RunSummary } from "../api/client";
import { withTicker } from "../lib/question";
import { DATA_NOTES, EXAMPLES } from "./examples";
import { HistoryCard } from "./History";
import { ResultView, type SavedRun } from "./ResultView";

const MAX_QUESTION = 2000;
type RunState = { state: "ready" | "running" | "complete" | "error"; label: string };
type Output =
  | { kind: "empty" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "result"; result: AgentResult; saved?: SavedRun };

interface Props {
  capabilities: Capabilities | "unavailable" | null;
  onOpenRun: () => void;
}

export function ResearchView({ capabilities, onOpenRun }: Props) {
  const caps = typeof capabilities === "object" ? capabilities : null;
  const kind = caps?.data_source.kind ?? "";
  const runtime = caps?.runtimes.find((r) => r.name === caps.default_runtime && r.supported);
  const modes = runtime?.modes ?? [caps?.default_mode ?? "single"];
  // Unreadable capabilities still try /runs, so the saved-run fallback says
  // why it's missing instead of staying empty (#122 review).
  const historyOn = caps ? caps.run_history : capabilities === "unavailable";

  const [question, setQuestion] = useState("");
  const [modeChoice, setMode] = useState<string | null>(null);
  const mode = modeChoice ?? caps?.default_mode ?? "single";
  const [runState, setRunState] = useState<RunState>({ state: "ready", label: "Ready" });
  const [output, setOutput] = useState<Output>({ kind: "empty" });
  const [runs, setRuns] = useState<RunSummary[] | string | null>(null);
  const [tickers, setTickers] = useState<string[]>([]);
  const [ticker, setTicker] = useState("");

  // The companies the tools have data for. Without the list the dropdown
  // just stays hidden: the question box still takes any company.
  useEffect(() => {
    api.companies().then(
      (list) => setTickers(list.companies.map((c) => c.company_id)),
      () => setTickers([]),
    );
  }, []);

  function pickTicker(next: string) {
    setTicker(next);
    setQuestion((current) => withTicker(current, next, tickers));
  }

  const loadHistory = useCallback(() => {
    if (!historyOn) return;
    api.runs().then(
      (list) => setRuns(list.runs),
      (error: Error) => setRuns(error.message),
    );
  }, [historyOn]);

  useEffect(loadHistory, [loadHistory]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const value = question.trim();
    if (!value) {
      setOutput({ kind: "error", message: "Enter a research question before starting." });
      setRunState({ state: "error", label: "Question required" });
      return;
    }
    setRunState({ state: "running", label: `Researching (${mode} mode can take minutes)` });
    setOutput({ kind: "loading" });
    try {
      const result = await api.investigate(value, mode);
      setOutput({ kind: "result", result, saved: historyOn ? { run_id: result.case_id } : undefined });
      setRunState(result.error ? { state: "error", label: "Completed with issue" } : { state: "complete", label: "Complete" });
      loadHistory();
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : "Could not reach the local research service. Make sure the app is running, then try again.";
      setOutput({ kind: "error", message });
      setRunState({ state: "error", label: "Request failed" });
    }
  }

  async function openRun(runId: string) {
    onOpenRun();
    try {
      const run = await api.run(runId);
      setQuestion(run.question);
      setOutput({
        kind: "result",
        result: run.result,
        saved: { run_id: run.run_id, created_at: run.created_at, feedback: run.feedback, replayed: true },
      });
      setRunState({ state: "complete", label: "Saved run" });
    } catch (error) {
      setOutput({ kind: "error", message: (error as Error).message });
      setRunState({ state: "error", label: "Couldn't open run" });
    }
  }

  const running = runState.state === "running";
  return (
    <>
      <section className="intro" aria-labelledby="page-title">
        <div className="eyebrow"><span className="eyebrow-line"></span> Live research · how an answer is produced</div>
        <h1 id="page-title">Financial research.<br /><span>Evidence you can inspect.</span></h1>
        <p className="intro-copy">
          Recon searches company financial facts and filing text, then shows each claim with the source it rests on,
          checked against what the tools actually returned.
        </p>
      </section>

      <section className="workspace" aria-label="Research workspace">
        <div className="side-column">
          <div className="question-card">
            <div className="card-heading">
              <div>
                <span className="section-index">01 / QUESTION</span>
                <h2>Ask the research agent</h2>
              </div>
            </div>
            <form onSubmit={submit}>
              <label className="visually-hidden" htmlFor="question">Research question</label>
              <textarea
                id="question"
                rows={4}
                maxLength={MAX_QUESTION}
                placeholder="Ask about a company's filings or financials"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
              />
              <div className="form-footer">
                <span>{question.length} / {MAX_QUESTION}</span>
                {tickers.length > 0 && (
                  <label className="mode-picker" htmlFor="company">
                    Company
                    <select id="company" value={ticker} onChange={(event) => pickTicker(event.target.value)}>
                      <option value="">Any company</option>
                      {tickers.map((t) => <option key={t} value={t}>{t}</option>)}
                    </select>
                  </label>
                )}
                <label className="mode-picker" htmlFor="mode">
                  Mode
                  <select id="mode" value={mode} onChange={(event) => setMode(event.target.value)}>
                    {modes.map((m) => <option key={m} value={m}>{m}</option>)}
                  </select>
                </label>
                <button id="submit-button" type="submit" disabled={running}>
                  <span className="button-label">{running ? "Researching…" : "Run research"}</span>
                  <span className="button-arrow" aria-hidden="true">↗</span>
                </button>
              </div>
            </form>
            <div className="examples">
              <span className="examples-label">TRY AN EXAMPLE</span>
              {(EXAMPLES[kind] ?? []).map(([label, text]) => (
                <button key={label} className="example-chip" type="button" onClick={() => setQuestion(text)}>
                  {label}<span>↗</span>
                </button>
              ))}
            </div>
            <aside className="data-note" aria-label="Data source notice">
              <span className="note-icon" aria-hidden="true">i</span>
              <p>
                {capabilities === "unavailable"
                  ? "Couldn't reach /capabilities. Check that the API is running."
                  : (DATA_NOTES[kind] ?? "Checking where the tools' data comes from…")}
              </p>
            </aside>
          </div>
          <HistoryCard enabled={historyOn} runs={runs} onRefresh={loadHistory} onOpen={openRun} />
        </div>

        <section className="answer-card" aria-labelledby="answer-heading" aria-live="polite" aria-busy={running}>
          <div className="answer-card-heading">
            <div>
              <span className="section-index">02 / FINDINGS</span>
              <h2 id="answer-heading">Research output</h2>
            </div>
            <span className="run-state" data-state={runState.state}>
              <span className="state-dot"></span> {runState.label}
            </span>
          </div>
          <div className="answer-content">
            <OutputView output={output} onFeedbackSaved={loadHistory} />
          </div>
        </section>
      </section>
    </>
  );
}

function OutputView({ output, onFeedbackSaved }: { output: Output; onFeedbackSaved: () => void }) {
  switch (output.kind) {
    case "empty":
      return (
        <div className="empty-state">
          <div className="empty-mark" aria-hidden="true"><span></span><span></span><span></span></div>
          <h3>Your research will appear here</h3>
          <p>Ask a question, or reopen a saved run, to see the answer, its claims and sources, and the tools the agent used.</p>
        </div>
      );
    case "loading":
      return <div className="empty-state loading-state">Running the research agent…</div>;
    case "error":
      return <div className="error-box">{output.message}</div>;
    case "result":
      // Keyed by run, so a new run starts with fresh feedback and closed
      // sources instead of the previous run's (review of #121).
      return (
        <ResultView
          key={output.saved?.run_id ?? output.result.case_id}
          result={output.result}
          saved={output.saved}
          onFeedbackSaved={onFeedbackSaved}
        />
      );
  }
}
