import { describe, expect, it } from "vitest";
import { withTicker } from "./question";

describe("withTicker", () => {
  it("starts an empty question with the ticker", () => {
    expect(withTicker("", "WDAY")).toBe("WDAY: ");
  });

  it("puts the ticker in front of a question that doesn't name it", () => {
    expect(withTicker("What was revenue in FY2024?", "AMD")).toBe("AMD: What was revenue in FY2024?");
  });

  it("swaps a ticker picked earlier for the new one", () => {
    expect(withTicker("WDAY: What was revenue?", "AMD")).toBe("AMD: What was revenue?");
  });

  it("leaves a question that already names the ticker alone", () => {
    expect(withTicker("How did NVDA's margin change?", "NVDA")).toBe("How did NVDA's margin change?");
  });

  it("clears an earlier pick when no company is chosen", () => {
    expect(withTicker("WDAY: What was revenue?", "")).toBe("What was revenue?");
  });
});
