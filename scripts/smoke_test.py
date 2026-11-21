#!/usr/bin/env python3
"""Safe local smoke test against the live Osmund Energi API.

Reads `OSMUND_API_KEY` from the process environment or a local `.env` file
(which is gitignored). Never prints the API key.

Usage:
    cp .env.example .env   # then paste your osm_... key into .env
    uv run python scripts/smoke_test.py
"""

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

import aiohttp

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "osmund_energi"


def _load_api_module() -> types.ModuleType:
    """Import `api.py` without requiring Home Assistant to be installed."""
    pkg_name = "osmund_energi_standalone"
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


def _load_dotenv(dotenv_path: Path) -> None:
    """Populate os.environ from a local .env file without overriding existing vars."""
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def _run() -> int:
    _load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("OSMUND_API_KEY", "").strip()
    if not api_key or api_key == "osm_your_api_key_here":
        print(
            "OSMUND_API_KEY is not set.\n"
            "Export OSMUND_API_KEY in your shell or copy .env.example to .env "
            "and add your key there.",
            file=sys.stderr,
        )
        return 1

    api = _load_api_module()

    async with aiohttp.ClientSession() as session:
        client = api.OsmundApiClient(api_key, session)
        try:
            key_details = await client.async_get_key_details()
            locations = await client.async_list_locations()
        except api.OsmundApiError as err:
            print(
                f"API error [{err.status} / {err.code}]: {err} "
                f"(requestId={err.request_id})",
                file=sys.stderr,
            )
            return 1

        print("Connected to Osmund Energi API successfully.")
        print(f"  Key name:   {key_details.name}")
        print(f"  Key prefix: {key_details.prefix}")
        print(f"  Expires at: {key_details.expires_at}")
        print(f"  Scopes:     {', '.join(key_details.scopes) or '(none)'}")

        if client.last_rate_limit is not None:
            rl = client.last_rate_limit
            print(
                f"  Rate limit: {rl.remaining}/{rl.limit} remaining "
                f"(resets in {rl.reset}s)"
            )

        print(f"\nLocations ({len(locations)}):")
        for loc in locations:
            label = loc.label or "(no label)"
            print(
                f"  - {label} [{loc.id}]\n"
                f"      state={loc.state}, plan={loc.plan}, "
                f"priceArea={loc.price_area}, apiAccess={loc.api_access}, "
                f"plus={loc.plus.status}\n"
                f"      meterResolution={loc.meter_resolution}, "
                f"consumptionThrough={loc.consumption_through}, "
                f"ratedThrough={loc.rated_through}"
            )

    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
