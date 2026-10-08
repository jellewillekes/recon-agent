import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { comparison, evalRun, evals } from "../../e2e/fixtures";
import { Compare, ComparisonTable } from "./Compare";
import { RunDetail } from "./EvalDetail";

describe("RunDetail", () => {
  it("flags a run that didn't score every case, and shows unscored cases as such", () => {
    const html = renderToStaticMarkup(<RunDetail run={evalRun} />);

    expect(html).toContain("Not every case was scored.");
    expect(html).toContain("1 case(s) the judge couldn&#x27;t score");
    expect(html).toContain("unscored");
    expect(html).toContain('data-failure="none"');
    expect(html).toContain("Failure classes over 1 case(s): Correct 1");
    expect(html).toContain("args 1.00");
  });
});

describe("ComparisonTable", () => {
  it("shows a verdict per gated metric for comparable runs", () => {
    const html = renderToStaticMarkup(<ComparisonTable result={comparison} />);

    expect(html).toContain("Comparable.");
    expect(html).toContain('data-verdict="better"');
    expect(html).toContain("0123456789ab…");
  });

  it("explains why the gate refuses runs it won't compare", () => {
    const refused = { ...comparison, comparable: false, reasons: ["rubric_version differs"] };
    const html = renderToStaticMarkup(<ComparisonTable result={refused} />);

    expect(html).toContain("The gate won&#x27;t compare these runs.");
    expect(html).toContain("rubric_version differs");
  });
});

describe("Compare", () => {
  it("won't compare a run with itself", () => {
    // Review of #127: with one recorded run, both sides preselect it.
    const [only] = evals.runs;
    const html = renderToStaticMarkup(<Compare runs={only ? [only] : []} />);

    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Compare<\/button>/);
    expect(html).toContain("Pick two different runs to compare.");
  });

  it("allows a comparison of two different runs", () => {
    const html = renderToStaticMarkup(<Compare runs={evals.runs} />);

    expect(html).not.toContain("disabled");
    expect(html).not.toContain("Pick two different runs");
  });
});
