"""Serving the web front end (#121, ADR 0033).

`web/` is a Vite build: `npm run build` writes static files to
`src/recon/api/static/`, which is gitignored and built into the image by the
Dockerfile's Node stage. Nothing runs Node at request time. Without a build
the root serves a short page saying how to make one, so the API itself works
in a Python-only checkout.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

WEB_DIR = Path(__file__).parent / "static"

_NOT_BUILT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Recon</title></head>
<body style="font-family: system-ui, sans-serif; margin: 3rem; max-width: 40rem">
<h1>The front end isn't built</h1>
<p>Run <code>make web</code> (needs Node 22 or later), then reload. The API is
running: see <a href="/docs">/docs</a>.</p>
</body></html>
"""


def mount_web(app: FastAPI, directory: Path = WEB_DIR) -> None:
    """Serve the built front end from `directory` at `/`, or the build notice
    when there's no `index.html` there. Mount last: it matches every path."""
    if (directory / "index.html").is_file():
        app.mount("/", StaticFiles(directory=directory, html=True), name="web")
        return

    @app.get("/", include_in_schema=False)
    async def not_built() -> HTMLResponse:
        return HTMLResponse(_NOT_BUILT)
