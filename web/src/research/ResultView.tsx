import type { AgentResult, Claim, Evidence, ToolCall } from "../api/client";
import type { components } from "../api/schema";
import { formatCost, formatCount, formatDuration } from "../lib/format";
import { RunActions } from "./History";

/** A result's run-store entry. A replayed run comes from the store, not a
 * live call. */
export interface SavedRun {
  run_id: string;
  created_at?: string;
  feedback?: components["schemas"]["StoredFeedback"] | null;
  replayed?: boolean;
}

interface Props {
  result: AgentResult;
  saved?: SavedRun;
  onFeedbackSaved: () => void;
}

export function ResultView({ result, saved, onFeedbackSaved }: Props) {
  const claims = result.claims ?? [];
  const calls = result.tool_calls ?? [];
  const tokens = (result.tokens_in ?? 0) + (result.tokens_out ?? 0);
  return (
    <div className="result">
      {saved?.replayed && saved.created_at && (
        <p className="replay-banner">
          Saved run from {new Date(saved.created_at).toLocaleString()} · reopened from the run store, no new agent call
        </p>
      )}
      <div className="result-meta">
        <span className="confidence" data-confidence={result.confidence}>{result.confidence} confidence</span>
        <span className="run-reference">Run {result.case_id}</span>
      </div>
      {result.error && <p className="result-warning">The agent reported an issue: {result.error}</p>}
      <p className="answer-text">{result.answer || "The agent returned no answer."}</p>
      <hr className="result-divider" />
      {claims.length ? (
        <Claims claims={claims} evidenceItems={result.evidence_items ?? []} />
      ) : (
        <EvidenceStrings evidence={result.evidence} />
      )}
      <hr className="result-divider" />
      <h3 className="detail-heading">
        Research trace <small>{plural(calls.length, "tool call")}</small>
      </h3>
      {calls.length ? (
        <div className="tool-list">
          {calls.map((call, index) => <ToolCallItem key={index} call={call} index={index} />)}
        </div>
      ) : (
        <p className="no-data">No data tools were called.</p>
      )}
      <div className="result-stats">
        <Stat label="Mode" value={result.mode} />
        <Stat label="Elapsed" value={formatDuration(result.elapsed_ms)} />
        <Stat label="Cost" value={formatCost(result.cost_eur)} />
        <Stat label="Tokens" value={formatCount(tokens)} />
      </div>
      {saved && <RunActions saved={saved} onSaved={onFeedbackSaved} />}
    </div>
  );
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

// Claims with their sources (ADR 0030). The source details come from the
// rows the tools returned; an unverified citation is shown as such.
function Claims({ claims, evidenceItems }: { claims: Claim[]; evidenceItems: Evidence[] }) {
  const byRef = new Map(evidenceItems.map((item) => [item.ref, item]));
  return (
    <>
      <h3 className="detail-heading">
        Claims and sources <small>{plural(claims.length, "claim")}</small>
      </h3>
      <ol className="claim-list">
        {claims.map((claim, index) => (
          <li key={index} className="claim-item" data-importance={claim.importance}>
            <p className="claim-text">{claim.text}</p>
            {claim.evidence_refs.length === 0 && <p className="no-data">No source cited.</p>}
            {claim.evidence_refs.map((ref) => (
              <Source key={ref} evidence={byRef.get(ref)} reference={ref} />
            ))}
          </li>
        ))}
      </ol>
    </>
  );
}

function Source({ evidence, reference }: { evidence?: Evidence; reference: string }) {
  const verified = Boolean(evidence?.verified);
  const parts = [
    evidence?.company_id,
    evidence?.form,
    evidence?.filed ? `filed ${evidence.filed}` : null,
    evidence?.section,
  ].filter(Boolean);
  return (
    <details className="source-item">
      <summary>
        <span className="source-badge" data-verified={String(verified)}>{verified ? "verified" : "unverified"}</span>
        <span className="source-label">{parts.join(" · ") || reference}</span>
      </summary>
      {verified ? (
        <>
          <blockquote className="source-excerpt">{evidence?.excerpt ?? ""}</blockquote>
          {evidence?.accession && <p className="source-meta">Accession {evidence.accession}</p>}
        </>
      ) : (
        <p className="source-meta">No tool returned {reference} in this run.</p>
      )}
    </details>
  );
}

function EvidenceStrings({ evidence }: { evidence: string[] }) {
  return (
    <>
      <h3 className="detail-heading">
        Evidence <small>{plural(evidence.length, "item")}</small>
      </h3>
      {evidence.length ? (
        <ul className="evidence-list">
          {evidence.map((item, index) => <li key={index} className="evidence-item">{item}</li>)}
        </ul>
      ) : (
        <p className="no-data">No evidence was returned for this answer.</p>
      )}
    </>
  );
}

function ToolCallItem({ call, index }: { call: ToolCall; index: number }) {
  return (
    <details className="tool-item">
      <summary>
        <span className="tool-name">{call.tool} · {index + 1}</span>
        <span className="tool-status" data-status={call.status}>{call.status.replaceAll("_", " ")}</span>
        <span className="tool-time">{formatDuration(call.elapsed_ms)}</span>
      </summary>
      <pre className="tool-arguments">{JSON.stringify(call.arguments ?? {}, null, 2)}</pre>
    </details>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <span>
      <strong>{label} </strong>
      {value}
    </span>
  );
}
