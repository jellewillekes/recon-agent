import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api, type Capabilities } from "./api/client";
import { EvaluationView } from "./evaluation/EvaluationView";
import { ResearchView } from "./research/ResearchView";

export type View = "research" | "evaluation";
const VIEWS: { id: View; label: string }[] = [
  { id: "research", label: "Research" },
  { id: "evaluation", label: "Evaluation" },
];

function viewFromHash(hash: string): View {
  return hash === "#evaluation" ? "evaluation" : "research";
}

export function App({ initialView }: { initialView?: View }) {
  const [view, setView] = useState<View>(initialView ?? viewFromHash(globalThis.location?.hash ?? ""));
  const [capabilities, setCapabilities] = useState<Capabilities | "unavailable" | null>(null);
  const tabs = useRef<Record<View, HTMLButtonElement | null>>({ research: null, evaluation: null });

  useEffect(() => {
    api.capabilities().then(setCapabilities, () => setCapabilities("unavailable"));
  }, []);

  function show(next: View, focus = false) {
    setView(next);
    if (location.hash !== `#${next}`) history.replaceState(null, "", `#${next}`);
    if (focus) tabs.current[next]?.focus();
  }

  // Arrow keys move between tabs, as the ARIA tabs pattern expects.
  function onTabKey(event: KeyboardEvent) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const index = VIEWS.findIndex((v) => v.id === view);
    const step = event.key === "ArrowRight" ? 1 : VIEWS.length - 1;
    const next = VIEWS[(index + step) % VIEWS.length];
    if (next) show(next.id, true);
  }

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Recon home">
          <span className="brand-mark" aria-hidden="true">R</span>
          <span>recon<span className="brand-dot">.</span></span>
        </a>
        <nav className="view-tabs" role="tablist" aria-label="Workspace views" onKeyDown={onTabKey}>
          {VIEWS.map(({ id, label }) => (
            <button
              key={id}
              ref={(node) => {
                tabs.current[id] = node;
              }}
              className="view-tab"
              type="button"
              role="tab"
              id={`tab-${id}`}
              aria-controls={`${id}-view`}
              aria-selected={view === id}
              tabIndex={view === id ? 0 : -1}
              onClick={() => show(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="topbar-meta">
          <DataSourceBadge capabilities={capabilities} />
        </div>
      </header>

      <main>
        <div id="research-view" role="tabpanel" aria-labelledby="tab-research" hidden={view !== "research"}>
          <ResearchView capabilities={capabilities} onOpenRun={() => show("research")} />
        </div>
        <div id="evaluation-view" role="tabpanel" aria-labelledby="tab-evaluation" hidden={view !== "evaluation"}>
          <EvaluationView active={view === "evaluation"} />
        </div>
        <footer className="page-footer">
          <span>RECON RESEARCH AGENT</span>
          <span>
            Local workspace <span className="footer-separator">·</span> Answers are generated, not investment advice
          </span>
        </footer>
      </main>
    </div>
  );
}

function DataSourceBadge({ capabilities }: { capabilities: Capabilities | "unavailable" | null }) {
  const caps = typeof capabilities === "object" ? capabilities : null;
  const kind = caps?.data_source.kind;
  const label =
    capabilities === null
      ? "Data source…"
      : capabilities === "unavailable"
        ? "Data source unknown"
        : kind === "edgar"
          ? "SEC EDGAR snapshot"
          : "Synthetic fixtures";
  return (
    <span
      className="local-badge"
      data-source={kind}
      title={caps ? `Tool data: ${caps.data_source.snapshot}` : "Where the tools' data comes from"}
    >
      <span className="pulse-dot"></span> <span>{label}</span>
    </span>
  );
}
