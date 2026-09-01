"""Smoke test for the src-layout packaging wired up in step 0."""

import importlib

import pytest


@pytest.mark.unit
def test_recon_package_imports() -> None:
    """The `recon` package installs editable via the src layout and imports cleanly."""
    module = importlib.import_module("recon")
    assert module.__file__ is not None
    assert "src/recon" in module.__file__.replace("\\", "/")
