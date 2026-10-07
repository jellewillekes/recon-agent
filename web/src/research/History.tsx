import { useState } from "react";
import { api, type RunSummary } from "../api/client";
import { formatCost } from "../lib/format";
import type { SavedRun } from "./ResultView";

interface HistoryProps {
  enabled: boolean;
  runs: RunSummary[] | string | null;
  onRefresh: () => void;
  onOpen: (runId: string) => void;
}

// Saved runs (GET /runs): reopen one without a live call.
export function HistoryCard({ enabled, runs, onRefresh, onOpen }: HistoryProps) {
  return (
    <section className="history-card" aria-labelledby="history-heading">
      <div className="card-heading">
        <div>
          <span className="section-index">03 / SAVED RUNS</span>
          <h2 id="history-heading">Reopen a run</h2>
        </div>
        <button className="link-button" type="button" onClick={onRefresh}>Refresh</button>
      </div>
      <p className="history-hint">Opens instantly from the run store. No new agent call.</p>
      <ul className="history-list">
        <HistoryItems enabled={enabled} runs={runs} onOpen={onOpen} />
      </ul>
    </section>
  );
}

function HistoryItems({ enabled, runs, onOpen }: Omit<HistoryProps, "onRefresh">) {
  if (!enabled) return <li className="no-data">Run history is off: the API has no DATABASE_URL.</li>;
  if (typeof runs === "string") return <li className="no-data">{runs}</li>;
  if (runs === null) return <li className="no-data">Loading saved runs…</li>;
  if (!runs.length) return <li className="no-data">No saved runs yet.</li>;
  return (
    <>
      {runs.map((run) => {
        const meta = [
          run.mode,
          new Date(run.created_at).toLocaleDateString(undefined, {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          }),
          formatCost(run.cost_eur),
          `${run.verified_evidence_count} verified`,
        ];
        if (run.feedback) meta.push(run.feedback === "up" ? "👍" : "👎");
        if (run.failed) meta.push("issue");
        return (
          <li key={run.run_id}>
            <button className="history-item" type="button" onClick={() => onOpen(run.run_id)}>
              <span className="history-question">{run.question}</span>
              <span className="history-meta">{meta.join(" · ")}</span>
            </button>
          </li>
        );
      })}
    </>
  );
}

// Export a saved run, and leave a 👍/👎 with a note (POST /runs/{id}/feedback).
export function RunActions({ saved, onSaved }: { saved: SavedRun; onSaved: () => void }) {
  const [note, setNote] = useState(saved.feedback?.note ?? "");
  const [status, setStatus] = useState(
    saved.feedback ? `Rated ${saved.feedback.rating === "up" ? "👍" : "👎"}` : "",
  );

  async function rate(rating: "up" | "down") {
    setStatus("Saving…");
    try {
      await api.feedback(saved.run_id, { rating, note });
      setStatus(`Saved ${rating === "up" ? "👍" : "👎"}`);
      onSaved();
    } catch (error) {
      setStatus((error as Error).message);
    }
  }

  return (
    <div className="run-actions">
      <button className="feedback-button" type="button" aria-label="Good answer" onClick={() => rate("up")}>👍</button>
      <button className="feedback-button" type="button" aria-label="Bad answer" onClick={() => rate("down")}>👎</button>
      <input
        className="feedback-note"
        type="text"
        maxLength={2000}
        placeholder="Optional note for the reviewers"
        aria-label="Feedback note"
        value={note}
        onChange={(event) => setNote(event.target.value)}
      />
      <a className="link-button" href={api.exportUrl(saved.run_id)} download>Export JSON</a>
      <span className="feedback-status" role="status">{status}</span>
    </div>
  );
}
