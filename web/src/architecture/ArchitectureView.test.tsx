import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ArchitectureView } from "./ArchitectureView";
import { docHref, SECTIONS } from "./sections";

// Keys of every markdown file under docs/, relative to this test file. The
// loaders are never called; the keys are enough to know a file exists.
const docs = Object.keys(import.meta.glob("../../../docs/**/*.md")).map((key) =>
  key.replace("../../../", ""),
);

describe("ArchitectureView", () => {
  it("renders the four sections, each with a diagram and a status", () => {
    const html = renderToStaticMarkup(<ArchitectureView />);

    for (const section of SECTIONS) {
      expect(html).toContain(section.title);
    }
    expect(SECTIONS.map((s) => s.id)).toEqual(["research", "verification", "evaluation", "service"]);
    expect(html.match(/<svg/g)).toHaveLength(SECTIONS.length);
    expect(html.match(/role="img"/g)).toHaveLength(SECTIONS.length);
  });

  it("gives every diagram a title and a description for screen readers", () => {
    const html = renderToStaticMarkup(<ArchitectureView />);

    expect(html.match(/<title[ >]/g)).toHaveLength(SECTIONS.length);
    expect(html.match(/<desc[ >]/g)).toHaveLength(SECTIONS.length);
  });

  it("links only to files that exist in the repo", () => {
    for (const section of SECTIONS) {
      expect(section.links.length, section.id).toBeGreaterThan(0);
      for (const link of section.links) {
        expect(docs, `${section.id}: ${link.path}`).toContain(link.path);
      }
    }
  });

  it("builds repo links from the path", () => {
    expect(docHref("docs/contracts.md")).toBe(
      "https://github.com/jellewillekes/recon-agent/blob/main/docs/contracts.md",
    );
  });

  it("marks what is not built, in the sections where something is missing", () => {
    const html = renderToStaticMarkup(<ArchitectureView />);

    expect(html).toContain("Not built yet");
    for (const id of ["research", "verification", "evaluation"]) {
      const section = SECTIONS.find((s) => s.id === id);
      expect(section?.notBuilt.length, id).toBeGreaterThan(0);
    }
  });

  it("uses plain wording, without product copy", () => {
    const html = renderToStaticMarkup(<ArchitectureView />).toLowerCase();

    for (const word of ["trustworthy", "explainable", "transparent", "responsible ai", "cutting-edge", "seamless", "powerful"]) {
      expect(html, word).not.toContain(word);
    }
  });
});
