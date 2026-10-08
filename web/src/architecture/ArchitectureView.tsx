import { Diagram } from "./Diagrams";
import { docHref, SECTIONS } from "./sections";

// How Recon works, written from what is in the repo. It calls no API.
export function ArchitectureView() {
  return (
    <>
      <section className="intro" aria-labelledby="arch-title">
        <div className="eyebrow"><span className="eyebrow-line"></span> How it works</div>
        <h1 id="arch-title">From question to verdict.<br /><span>What is built, and what isn't.</span></h1>
        <p className="intro-copy">
          Four parts: research, verification, evaluation and the service that runs them. Each lists what exists today and
          what doesn't, with links to the decision records and docs.
        </p>
      </section>

      {SECTIONS.map((section) => (
        <section key={section.id} className="arch-card" aria-labelledby={`arch-${section.id}`}>
          <span className="section-index">{section.index}</span>
          <h2 id={`arch-${section.id}`}>{section.title}</h2>
          <p className="arch-summary">{section.summary}</p>
          <Diagram id={section.id} />
          <div className="arch-columns">
            <div>
              <h3>What exists</h3>
              <ul>
                {section.points.map((point) => (
                  <li key={point}>{point}</li>
                ))}
              </ul>
            </div>
            <div>
              <h3>Not built yet</h3>
              <ul className="arch-missing">
                {section.notBuilt.map((point) => (
                  <li key={point}>{point}</li>
                ))}
              </ul>
              <h3>Read more</h3>
              <ul className="arch-links">
                {section.links.map((link) => (
                  <li key={link.path}>
                    <a href={docHref(link.path)} target="_blank" rel="noreferrer">
                      {link.label}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>
      ))}
    </>
  );
}
