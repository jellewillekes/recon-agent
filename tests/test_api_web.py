"""Serving the web front end (#121, ADR 0033) and the OpenAPI schema its
typed client is generated from. The front end itself is tested in `web/`
(Vitest and Playwright)."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from recon.api import main as api_main
from recon.api.web import mount_web

pytestmark = [pytest.mark.unit, pytest.mark.anyio]

OPENAPI_JSON = Path(__file__).resolve().parent.parent / "web" / "openapi.json"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _get(app: FastAPI, path: str) -> tuple[int, str, str]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(path)
    return resp.status_code, resp.headers.get("content-type", ""), resp.text


async def test_a_built_front_end_is_served_at_the_root(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><div id='root'></div>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('recon')")
    app = FastAPI()
    mount_web(app, tmp_path)

    status, content_type, body = await _get(app, "/")
    assert status == 200 and "text/html" in content_type
    assert "id='root'" in body
    status, _, body = await _get(app, "/assets/app.js")
    assert status == 200 and "recon" in body


async def test_without_a_build_the_root_says_how_to_build_it(tmp_path: Path) -> None:
    app = FastAPI()
    mount_web(app, tmp_path / "missing")

    status, content_type, body = await _get(app, "/")

    assert status == 200 and "text/html" in content_type
    assert "make web" in body


async def test_the_api_still_answers_next_to_the_front_end() -> None:
    status, _, body = await _get(api_main.app, "/healthz")
    assert status == 200 and "ok" in body


def test_the_committed_openapi_schema_matches_the_api() -> None:
    """The typed client is generated from web/openapi.json, so a contract
    change breaks the front end's build instead of the page. Regenerate with
    `uv run python scripts/export_openapi.py` and `npm run api:types`."""
    committed = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    assert committed == api_main.app.openapi()


@pytest.mark.parametrize(
    ("path", "method", "schema"),
    [
        ("/investigate", "post", "AgentResult"),
        ("/runs", "get", "RunList"),
        ("/runs/{run_id}", "get", "ResearchRun"),
        ("/evals", "get", "EvalList"),
        ("/evals/{run_id}", "get", "EvalRun"),
        ("/evals/compare", "get", "Comparison"),
        ("/capabilities", "get", "Capabilities"),
        ("/companies", "get", "CompanyList"),
    ],
)
def test_every_endpoint_the_page_reads_documents_its_response(
    path: str, method: str, schema: str
) -> None:
    operation = api_main.app.openapi()["paths"][path][method]
    content = operation["responses"]["200"]["content"]["application/json"]
    assert content["schema"]["$ref"].endswith(f"/{schema}")
