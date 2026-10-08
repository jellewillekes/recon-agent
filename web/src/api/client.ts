// Typed calls to the API. The types come from web/openapi.json, generated
// from the FastAPI app (`npm run api:types`), so a contract change fails
// `tsc` here instead of breaking the page (#121).
import type { components } from "./schema";
import { describeError } from "../lib/format";

type Schemas = components["schemas"];
export type AgentResult = Schemas["AgentResult"];
export type Claim = Schemas["Claim"];
export type Evidence = Schemas["Evidence"];
export type ToolCall = Schemas["ToolCall"];
export type Capabilities = Schemas["Capabilities"];
export type Company = Schemas["Company"];
export type RunSummary = Schemas["RunSummary"];
export type RunList = Schemas["RunList"];
export type ResearchRun = Schemas["ResearchRun"];
export type EvalSummary = Schemas["EvalSummary"];
export type EvalList = Schemas["EvalList"];
export type EvalRun = Schemas["EvalRun"];
export type CaseScore = Schemas["CaseScore"];
export type TrajectoryScore = Schemas["TrajectoryScore"];
export type Comparison = Schemas["Comparison"];
export type Feedback = Schemas["Feedback"];

/** An API error with the message the page should show. */
export class ApiError extends Error {}

async function request<T>(path: string, fallback: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(describeError(payload, `${fallback} (${response.status})`));
  }
  return payload as T;
}

function post<T>(path: string, body: unknown, fallback: string): Promise<T> {
  return request<T>(path, fallback, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

const id = encodeURIComponent;

export const api = {
  capabilities: () => request<Capabilities>("/capabilities", "Couldn't load capabilities"),
  companies: () => request<{ companies: Company[] }>("/companies", "Couldn't load the company list"),
  investigate: (question: string, mode: string) =>
    post<AgentResult>("/investigate", { question, context: {}, mode }, "The request failed. Try again"),
  runs: () => request<RunList>("/runs?limit=20", "Couldn't load saved runs"),
  run: (runId: string) => request<ResearchRun>(`/runs/${id(runId)}`, "Couldn't open that run"),
  feedback: (runId: string, feedback: Feedback) =>
    post<{ status: string }>(`/runs/${id(runId)}/feedback`, feedback, "Feedback wasn't saved"),
  exportUrl: (runId: string) => `/runs/${id(runId)}/export`,
  evals: () => request<EvalList>("/evals", "Couldn't load eval runs"),
  evalRun: (runId: string) => request<EvalRun>(`/evals/${id(runId)}`, "Couldn't load that run"),
  compare: (baseline: string, candidate: string) =>
    request<Comparison>(
      `/evals/compare?${new URLSearchParams({ baseline, candidate })}`,
      "Couldn't compare those runs",
    ),
};
