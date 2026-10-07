// Synthetic API responses (fictional FIRM-001), shared by the Vitest
// component tests and the Playwright smoke tests. No real data.
import type {
  AgentResult,
  Capabilities,
  Comparison,
  EvalList,
  EvalRun,
  ResearchRun,
  RunList,
} from "../../src/api/client";
import capabilitiesJson from "./capabilities.json" with { type: "json" };
import compareJson from "./compare.json" with { type: "json" };
import evalRunJson from "./eval-run.json" with { type: "json" };
import evalsJson from "./evals.json" with { type: "json" };
import resultJson from "./result.json" with { type: "json" };
import runJson from "./run.json" with { type: "json" };
import runsJson from "./runs.json" with { type: "json" };

// JSON imports widen literal types (e.g. "high" to string); the API's own
// tests pin the real shapes, so a cast is enough here.
export const capabilities = capabilitiesJson as unknown as Capabilities;
export const result = resultJson as unknown as AgentResult;
export const runs = runsJson as unknown as RunList;
export const run = runJson as unknown as ResearchRun;
export const evals = evalsJson as unknown as EvalList;
export const evalRun = evalRunJson as unknown as EvalRun;
export const comparison = compareJson as unknown as Comparison;
