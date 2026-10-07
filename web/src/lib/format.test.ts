import { describe, expect, it } from "vitest";
import { describeError, formatCost, formatDuration, number, percent } from "./format";

describe("format", () => {
  it("shows durations in ms below a second and seconds above", () => {
    expect(formatDuration(850)).toBe("850ms");
    expect(formatDuration(30_000)).toBe("30.0s");
    expect(formatDuration(undefined)).toBe("—");
  });

  it("shows costs in euros to at most four decimals", () => {
    expect(formatCost(0.1234)).toContain("0.1234");
    expect(formatCost(null)).toBe("—");
  });

  it("shows missing numbers as a dash", () => {
    expect(number(0.56789)).toBe("0.568");
    expect(number(null)).toBe("—");
    expect(percent(0.75)).toBe("75%");
    expect(percent(undefined)).toBe("—");
  });

  it("explains an API error from its detail, its fields, or a fallback", () => {
    expect(describeError({ detail: "No eval run 'x'." }, "fallback")).toBe("No eval run 'x'.");
    expect(describeError({ fields: ["question"] }, "fallback")).toBe("Check the question fields: question.");
    expect(describeError({}, "fallback")).toBe("fallback");
  });
});
