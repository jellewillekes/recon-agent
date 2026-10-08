import type { ReactNode } from "react";
import type { SectionId } from "./sections";

// Inline SVG, drawn with the page's CSS variables so a diagram follows the
// theme. Dashed boxes are things that are not built.

interface BoxProps {
  x: number;
  y: number;
  w: number;
  h?: number;
  lines: string[];
  kind?: "plain" | "accent" | "absent";
}

function Box({ x, y, w, h = 54, lines, kind = "plain" }: BoxProps) {
  const first = y + h / 2 - ((lines.length - 1) * 8) - 1;
  return (
    <g className={`dg-box dg-${kind}`}>
      <rect x={x} y={y} width={w} height={h} rx={9} />
      {lines.map((line, i) => (
        <text key={line} x={x + w / 2} y={first + i * 16 + 5} textAnchor="middle" className={i === 0 ? "dg-title" : "dg-sub"}>
          {line}
        </text>
      ))}
    </g>
  );
}

function Arrow({ from, to, dashed = false }: { from: [number, number]; to: [number, number]; dashed?: boolean }) {
  return (
    <line
      x1={from[0]}
      y1={from[1]}
      x2={to[0]}
      y2={to[1]}
      className={dashed ? "dg-line dg-dashed" : "dg-line"}
      markerEnd="url(#arrow)"
    />
  );
}

function Frame({ id, title, desc, height, children }: { id: SectionId; title: string; desc: string; height: number; children: ReactNode }) {
  return (
    <div className="dg-scroll">
      <svg className="dg" viewBox={`0 0 760 ${height}`} role="img" aria-labelledby={`dg-${id}-t dg-${id}-d`}>
        <title id={`dg-${id}-t`}>{title}</title>
        <desc id={`dg-${id}-d`}>{desc}</desc>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L10 5 L0 10 z" className="dg-head" />
          </marker>
        </defs>
        {children}
      </svg>
    </div>
  );
}

function Research() {
  return (
    <Frame
      id="research"
      height={290}
      title="Research flow"
      desc="A question goes to the API and a runtime. The runtime calls MCP tools, which read XBRL facts in DuckDB and filing text in Postgres. The answer returns with claims and evidence."
    >
      <Box x={8} y={24} w={130} lines={["Question", "analyst, web app"]} />
      <Box x={198} y={24} w={150} lines={["API", "POST /investigate"]} />
      <Box x={408} y={24} w={160} lines={["Runtime", "single or multi"]} kind="accent" />
      <Box x={628} y={24} w={124} lines={["Answer", "claims + evidence"]} />
      <Arrow from={[138, 51]} to={[198, 51]} />
      <Arrow from={[348, 51]} to={[408, 51]} />
      <Arrow from={[568, 51]} to={[628, 51]} />
      <Box x={388} y={130} w={200} lines={["MCP tools", "5 read tools, 1 write"]} />
      <Arrow from={[488, 78]} to={[488, 130]} />
      <Box x={268} y={226} w={210} lines={["DuckDB", "XBRL facts, filings"]} />
      <Box x={498} y={226} w={240} lines={["Postgres", "filing text: pgvector, full text"]} />
      <Arrow from={[448, 184]} to={[400, 226]} />
      <Arrow from={[528, 184]} to={[590, 226]} />
      <Box x={138} y={130} w={190} lines={["Budgets", "calls, tokens, time"]} />
      <Arrow from={[430, 78]} to={[328, 150]} />
    </Frame>
  );
}

function Verification() {
  return (
    <Frame
      id="verification"
      height={290}
      title="Claim to verdict"
      desc="A claim cites row refs. The server resolves each ref to a tool row, verified or not. The numeric verifier recomputes the claim from those rows and returns a verdict. A model-based verifier for causes is not built."
    >
      <Box x={8} y={24} w={150} lines={["Claim", "text + cited refs"]} />
      <Box x={218} y={24} w={170} lines={["Evidence", "ref matches a tool row?"]} />
      <Box x={448} y={24} w={150} lines={["Numeric verifier", "recompute, compare"]} kind="accent" />
      <Box x={658} y={24} w={94} lines={["Verdict"]} />
      <Arrow from={[158, 51]} to={[218, 51]} />
      <Arrow from={[388, 51]} to={[448, 51]} />
      <Arrow from={[598, 51]} to={[658, 51]} />
      <Box x={448} y={130} w={304} h={64} lines={["SUPPORTED · CONTRADICTED · STALE", "UNSUPPORTED · UNVERIFIABLE"]} />
      <Arrow from={[705, 78]} to={[705, 130]} />
      <Box x={8} y={130} w={380} h={64} lines={["POST /verify", "claims + rows in, VerificationReport out"]} />
      <Arrow from={[300, 130]} to={[470, 78]} />
      <Box x={448} y={226} w={304} lines={["Model-based verifier", "causes, outlook: not built"]} kind="absent" />
      <Arrow from={[598, 226]} to={[598, 200]} dashed />
    </Frame>
  );
}

function Evaluation() {
  return (
    <Frame
      id="evaluation"
      height={290}
      title="Evaluation loop"
      desc="Benchmark cases run through a runtime and produce results. A rubric judge and checks without a model score them. The scores are totalled into a run file, and the gate compares it with the committed baseline."
    >
      <Box x={8} y={24} w={130} lines={["Cases", "finance-agent-bench"]} />
      <Box x={198} y={24} w={140} lines={["Runtime", "per case"]} kind="accent" />
      <Box x={398} y={24} w={140} lines={["AgentResult", "answer, claims, trace"]} />
      <Box x={598} y={24} w={154} lines={["Scoring", "judge + checks"]} />
      <Arrow from={[138, 51]} to={[198, 51]} />
      <Arrow from={[338, 51]} to={[398, 51]} />
      <Arrow from={[538, 51]} to={[598, 51]} />
      <Box x={598} y={130} w={154} lines={["Run file", "evals/results"]} />
      <Arrow from={[675, 78]} to={[675, 130]} />
      <Box x={348} y={130} w={190} lines={["Gate", "same inputs? within noise?"]} kind="accent" />
      <Arrow from={[598, 157]} to={[538, 157]} />
      <Box x={98} y={130} w={190} lines={["Baseline", "evals/baseline.json"]} />
      <Arrow from={[288, 157]} to={[348, 157]} />
      <Box x={348} y={226} w={190} lines={["Pass or fail", "with the reasons"]} />
      <Arrow from={[443, 184]} to={[443, 226]} />
      <Box x={8} y={226} w={300} lines={["Not measured yet", "claim verdicts, multi mode, LangGraph"]} kind="absent" />
    </Frame>
  );
}

function Service() {
  return (
    <Frame
      id="service"
      height={290}
      title="Service"
      desc="The browser talks to a FastAPI service. The service uses Postgres and the MCP tools. It exposes metrics to Prometheus and sends traces to Tempo. Grafana reads both."
    >
      <Box x={8} y={24} w={130} lines={["Browser", "this web app"]} />
      <Box x={218} y={24} w={200} h={78} lines={["FastAPI", "/investigate  /verify  /evals", "/healthz  /readyz  /metrics"]} kind="accent" />
      <Arrow from={[138, 63]} to={[218, 63]} />
      <Box x={518} y={8} w={234} lines={["Postgres", "saved runs, flags, vectors"]} />
      <Box x={518} y={82} w={234} lines={["MCP server", "tools over DuckDB data"]} />
      <Arrow from={[418, 50]} to={[518, 35]} />
      <Arrow from={[418, 78]} to={[518, 109]} />
      <Box x={158} y={196} w={150} lines={["Prometheus", "scrapes /metrics"]} />
      <Box x={348} y={196} w={150} lines={["Tempo", "OpenTelemetry traces"]} />
      <Box x={578} y={196} w={174} lines={["Grafana", "dashboards"]} />
      <Arrow from={[290, 102]} to={[233, 196]} />
      <Arrow from={[346, 102]} to={[423, 196]} />
      <path d="M233 250 V274 H665 V250" className="dg-line" markerEnd="url(#arrow)" />
      <Arrow from={[498, 223]} to={[578, 223]} />
    </Frame>
  );
}

const DIAGRAMS: Record<SectionId, () => ReactNode> = {
  research: Research,
  verification: Verification,
  evaluation: Evaluation,
  service: Service,
};

export function Diagram({ id }: { id: SectionId }) {
  const Component = DIAGRAMS[id];
  return <Component />;
}
