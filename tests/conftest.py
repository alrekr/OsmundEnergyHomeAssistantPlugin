"""Shared pytest fixtures for Osmund Energi unit tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "osmund_energi"


@pytest.fixture(scope="session")
def osmund_api() -> types.ModuleType:
    """Load `api.py` standalone so unit tests run without Home Assistant installed."""
    pkg_name = "osmund_energi_test_pkg"
    if f"{pkg_name}.api" in sys.modules:
        return sys.modules[f"{pkg_name}.api"]

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(INTEGRATION_DIR)]
    sys.modules[pkg_name] = pkg

    for mod_name in ("const", "api"):
        full_name = f"{pkg_name}.{mod_name}"
        spec = importlib.util.spec_from_file_location(
            full_name, INTEGRATION_DIR / f"{mod_name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)

    return sys.modules[f"{pkg_name}.api"]
