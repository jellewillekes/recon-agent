# 0037: The architecture page is a tab, written from what exists

## Context

#136 asks for a page that explains how Recon works, so the repo and the web app read as
one project. The app (ADR 0033) has no router: the Research and Evaluation views are tab
panels chosen by the URL hash, and the API serves the build as static files. The issue
also asks for diagrams that work in light and dark, and for no claims the repo can't back.

## Decision

- The page is a third tab, `#architecture`, in the existing tablist. It calls no API. The
  issue's "Runs" nav item stays where it is, inside the Research view.
- Four sections: research, verification, evaluation and the service. Each has an inline SVG
  diagram, a list of what exists, a list of what is not built, and links to docs and ADRs.
- What is not built goes in its own list, and unbuilt parts in a diagram are dashed. A
  point under "What exists" describes something in the repo today.
- Diagrams use the page's CSS variables, so they follow whatever theme the app has. The app
  has only a light theme, so no dark palette is added here.
- Links go to the files on GitHub. A test checks that every linked file exists under
  `docs/`, and a Playwright test checks the link targets. Neither follows a link on the
  network.

## Consequences

- The text is hand-written and can fall behind the code. The "not built" lists name the
  issues that will change them (#139), and they need editing when those close.
- Without a router there is no `/architecture` path. The hash link `#architecture` works.
- A dark theme for the whole app is a separate piece of work.
