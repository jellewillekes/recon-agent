"""Suite-wide test setup."""

import pytest

from recon.tools.data_source import TOOL_DATA_ENV


@pytest.fixture(autouse=True)
def _tool_data_is_the_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's MCP server subprocess reads the synthetic fixture.

    Runtimes pass the parent environment to the subprocess they spawn, and
    the production default is real SEC EDGAR data, which CI doesn't have and
    whose companies no test expects. Tests that exercise the data source
    choice itself override this.
    """
    monkeypatch.setenv(TOOL_DATA_ENV, "fixture")
