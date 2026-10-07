import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { result } from "../../e2e/fixtures";
import { ResultView } from "./ResultView";

const render = (props: Partial<Parameters<typeof ResultView>[0]> = {}) =>
  renderToStaticMarkup(<ResultView result={result} onFeedbackSaved={() => {}} {...props} />);

describe("ResultView", () => {
  it("shows each claim with its source, verified or not", () => {
    const html = render();

    expect(html).toContain("Revenue was 120 million in FY2024.");
    expect(html).toContain('data-verified="true"');
    expect(html).toContain("FIRM-001 · 10-K · filed 2025-02-01");
    expect(html).toContain("Revenue FY2024 = 120000000");
    expect(html).toContain('data-verified="false"');
    expect(html).toContain("No tool returned E000000000bb2 in this run.");
  });

  it("shows the trace, mode, time, cost and tokens", () => {
    const html = render();

    expect(html).toContain("2 tool calls");
    expect(html).toContain("get_financial_fact · 2");
    expect(html).toContain("31.0s");
    expect(html).toContain("4,300");
  });

  it("falls back to evidence strings when there are no claims", () => {
    const html = render({ result: { ...result, claims: [], evidence: ["a filing line"] } });

    expect(html).toContain("a filing line");
    expect(html).not.toContain("Claims and sources");
  });

  it("offers feedback and export only for a saved run, and marks a replay", () => {
    expect(render()).not.toContain("Export JSON");
    const saved = render({
      saved: { run_id: "api-saved1", created_at: "2026-10-07T10:00:00Z", replayed: true },
    });
    expect(saved).toContain("Export JSON");
    expect(saved).toContain('href="/runs/api-saved1/export"');
    expect(saved).toContain("no new agent call");
  });
});
