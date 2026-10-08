// What the architecture page says. Each point describes something in the repo
// today; what isn't built goes in `notBuilt`, not in `points`. Every link
// points at a file under docs/, and a test checks the file exists.

export const REPO = "https://github.com/jellewillekes/recon-agent";

export function docHref(path: string): string {
  return `${REPO}/blob/main/${path}`;
}

export type SectionId = "research" | "verification" | "evaluation" | "service";

export interface Section {
  id: SectionId;
  index: string;
  title: string;
  summary: string;
  points: string[];
  notBuilt: string[];
  links: { label: string; path: string }[];
}

export const SECTIONS: Section[] = [
  {
    id: "research",
    index: "01 / RESEARCH",
    title: "How a question becomes an answer",
    summary:
      "A question goes to the API. A runtime runs the agent, which reads data only through MCP tools. The answer comes back with claims that cite the rows the tools returned.",
    points: [
      "Two modes: single (one investigator) or multi (a supervisor, two workers and a critic). The service runs both on the Claude Agent SDK.",
      "Five read tools: list_companies, list_financial_concepts, get_financial_fact, search_filings and search_knowledge. One write tool, flag_case_for_review, needs a confirmation step.",
      "Numbers come from XBRL facts queried with DuckDB over a SEC EDGAR snapshot, or synthetic fixtures. Filing text is searched in Postgres with pgvector, full-text search and a reranker.",
      "Tools return one of five statuses instead of raising. Each run has budgets for tool calls, tokens and time.",
    ],
    notBuilt: [
      "LangGraph is a second runtime, but this service doesn't run it. It needs a metered API key (ADR 0027).",
    ],
    links: [
      { label: "Runtimes", path: "docs/runtimes.md" },
      { label: "Data sources", path: "docs/data-sources.md" },
      { label: "ADR 0009: tool reliability and run budgets", path: "docs/adr/0009-tool-reliability-and-run-budgets.md" },
      { label: "ADR 0025: retrieval over filing text", path: "docs/adr/0025-retrieval-over-filing-text.md" },
    ],
  },
  {
    id: "verification",
    index: "02 / VERIFICATION",
    title: "From claim to verdict",
    summary:
      "An answer is a list of claims. Each claim cites rows by ref, and the server checks that each ref matches a row a tool returned. A separate verifier recomputes numeric claims from those rows.",
    points: [
      "A ref is a hash of the tool name and the row. A made-up citation stays in the result, marked unverified.",
      "The numeric verifier reads levels, growth rates and ratios and recomputes them. It returns SUPPORTED, CONTRADICTED, STALE, UNSUPPORTED or UNVERIFIABLE, and rounds to the precision the claim states.",
      "A claim that gives a cause, an outlook or a negation is UNVERIFIABLE. It is not passed on its number.",
      "POST /verify takes claims and rows and returns a report. The promotion gate can fail a candidate with more unsupported or contradicted claims.",
    ],
    notBuilt: [
      "Evaluation runs don't produce verdicts yet, so the gate's claim check has no data (#139).",
      "Causes and outlook need a model-based verifier, which doesn't exist. PARTIALLY_SUPPORTED is a label in the test set only.",
      "The Research view shows citations as verified or not. It doesn't show verdicts.",
    ],
    links: [
      { label: "ADR 0030: claims cite tool rows by ref", path: "docs/adr/0030-claims-cite-tool-rows-by-ref.md" },
      { label: "ADR 0034: numeric claim verification", path: "docs/adr/0034-numeric-claim-verification.md" },
      { label: "ADR 0035: report, endpoint and claim gate", path: "docs/adr/0035-verification-report-and-claim-gate.md" },
      { label: "Contracts", path: "docs/contracts.md" },
    ],
  },
  {
    id: "evaluation",
    index: "03 / EVALUATION",
    title: "How a version is judged",
    summary:
      "Fixed benchmark cases run through a runtime. Scores come from a rubric judge and from checks that use no model. A gate compares a candidate run with the committed baseline.",
    points: [
      "Cases come from finance-agent-bench. The judge runs on Haiku 4.5, and a run stops at a cost cap of €1 by default.",
      "Each case gets an answer score, a tool path check, citation checks, a run-path breakdown and one failure class: retrieval, reasoning, tool use, budget or runtime error.",
      "The gate refuses to compare runs that measured something different: rubric, dataset, tool data or case set. It fails a drop in task completion or answer score beyond the noise band, and a cost rise without more cases completing.",
      "Results are files in evals/results, so reading them costs nothing. Headline scores carry 95% intervals. The noise band is a fixed value set by the owner.",
    ],
    notBuilt: [
      "The baseline is one run: Claude Agent SDK, single mode, 7 cases. Multi mode and LangGraph aren't measured on the current rubric.",
      "Run-to-run noise is measured from one pair of runs, and it is wider than the gate's band.",
    ],
    links: [
      { label: "ADR 0018: run comparability", path: "docs/adr/0018-run-comparability-in-the-gate.md" },
      { label: "ADR 0028: the gate's noise band", path: "docs/adr/0028-gate-noise-band.md" },
      { label: "ADR 0031: failure classes", path: "docs/adr/0031-failure-classes.md" },
      { label: "ADR 0036: intervals and the noise rule", path: "docs/adr/0036-intervals-and-the-noise-rule.md" },
      { label: "Run-to-run noise", path: "docs/eval-noise.md" },
    ],
  },
  {
    id: "service",
    index: "04 / SERVICE",
    title: "How it runs",
    summary:
      "A FastAPI service serves the API and this web app. State lives in Postgres, metrics go to Prometheus and traces go through OpenTelemetry to Grafana.",
    points: [
      "GET /healthz checks only that the process is up. GET /readyz checks the MCP server and Postgres.",
      "GET /metrics exposes request counts, latency and runs in flight in Prometheus format.",
      "Every request has an X-Request-ID, which appears on the spans of its trace.",
      "Saved research runs are stored in Postgres. The container image is scanned in CI, and a Helm chart runs it on k3d.",
    ],
    notBuilt: ["The Helm chart has no Ingress. The k3d flow uses port-forward."],
    links: [
      { label: "Observability", path: "docs/observability.md" },
      { label: "Deployment", path: "docs/deployment.md" },
      { label: "ADR 0003: API service dependencies", path: "docs/adr/0003-api-service-dependencies.md" },
      { label: "ADR 0019: image scan", path: "docs/adr/0019-image-scan.md" },
      { label: "ADR 0023: tracing at the harness boundary", path: "docs/adr/0023-tracing-at-the-harness-boundary.md" },
    ],
  },
];
