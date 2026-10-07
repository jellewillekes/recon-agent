"""Suite-wide test setup."""

import pytest

from recon.tools.data_source import TOOL_DATA_ENV
from recon.tracing import ENDPOINT_ENV


@pytest.fixture(autouse=True)
def _tool_data_is_the_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's MCP server subprocess reads the synthetic fixture.

    Runtimes pass the parent environment to the subprocess they spawn, and
    the production default is real SEC EDGAR data, which CI doesn't have and
    whose companies no test expects. Tests that exercise the data source
    choice itself override this.
    """
    monkeypatch.setenv(TOOL_DATA_ENV, "fixture")


@pytest.fixture(autouse=True)
def _no_trace_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never export spans over the network, even when the developer's
    shell points OTEL_EXPORTER_OTLP_ENDPOINT at a running Tempo."""
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests start without either API key, even when the developer's shell
    exports one. The tests that need a key set a fake one (ADR 0027)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RECON_ANTHROPIC_API_KEY", raising=False)
