import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { App } from "./App";

describe("App", () => {
  it("renders the research and evaluation views as labelled tab panels", () => {
    const html = renderToStaticMarkup(<App initialView="research" />);

    expect(html).toContain("Financial research.");
    expect(html).toContain("Evidence you can inspect.");
    expect(html).toContain('id="research-view"');
    expect(html).toContain('id="evaluation-view"');
    expect(html).toContain("Benchmark results · not live answers");
    expect(html).toMatch(/id="tab-research"[^>]*aria-selected="true"/);
  });

  it("adds the architecture view as a third tab panel", () => {
    const html = renderToStaticMarkup(<App initialView="architecture" />);

    expect(html).toContain('id="architecture-view"');
    expect(html).toMatch(/id="tab-architecture"[^>]*aria-selected="true"/);
    expect(html).toMatch(/id="research-view"[^>]*hidden/);
    expect(html).not.toMatch(/id="architecture-view"[^>]*hidden/);
  });

  it("hides the view that isn't selected", () => {
    const html = renderToStaticMarkup(<App initialView="evaluation" />);

    expect(html).toMatch(/id="research-view"[^>]*hidden/);
    expect(html).not.toMatch(/id="evaluation-view"[^>]*hidden/);
  });

  it("puts no credential name in the shipped source", () => {
    const sources = import.meta.glob("./**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });
    for (const [path, text] of Object.entries(sources)) {
      if (path.endsWith(".test.tsx")) continue;
      expect(text, path).not.toMatch(/CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY/);
    }
  });
});
