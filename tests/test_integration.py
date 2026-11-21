"""Unit tests for translations, diagnostics redaction, and coordinator caching."""

import ast
import importlib
import json
import sys
import types
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "osmund_energi"


def test_translations_cover_all_sensor_keys_and_selectors() -> None:
    """Ensure every sensor translation_key and selector option is in en.json."""
    translations = json.loads(
        (INTEGRATION_DIR / "translations" / "en.json").read_text(encoding="utf-8")
    )

    sensor_ast = ast.parse(
        (INTEGRATION_DIR / "sensor.py").read_text(encoding="utf-8")
    )
    sensor_keys: set[str] = set()
    for node in ast.walk(sensor_ast):
        if isinstance(node, ast.keyword) and node.arg == "translation_key":
            assert isinstance(node.value, ast.Constant)
            assert isinstance(node.value.value, str)
            sensor_keys.add(node.value.value)

    assert len(sensor_keys) >= 25
    en_sensors = translations["entity"]["sensor"]
    missing = sensor_keys - set(en_sensors)
    assert not missing, f"Missing sensor translations: {missing}"

    # Verify enum states are lowercase and present
    for state in ("active", "upcoming", "pending", "ended"):
        assert state in en_sensors["supply_state"]["state"]
    for state in ("included", "trial", "active", "none"):
        assert state in en_sensors["plus_status"]["state"]
    for state in ("hourly", "quarter_hourly"):
        assert state in en_sensors["meter_resolution"]["state"]

    # Verify selector options match const.py & services.yaml
    selectors = translations["selector"]
    assert set(selectors["price_resolution"]["options"]) == {
        "auto",
        "hourly",
        "quarter_hourly",
    }
    assert set(selectors["service_price_day"]["options"]) == {
        "today",
        "tomorrow",
        "both",
    }
    assert set(selectors["service_price_resolution"]["options"]) == {
        "hourly",
        "quarter_hourly",
    }

    # Verify service translations match services.yaml fields
    svc = translations["services"]["get_prices"]
    assert set(svc["fields"]) == {
        "config_entry_id",
        "location_id",
        "day",
        "resolution",
    }

    # Verify Danish translation has exact structural & placeholder parity with English
    da_translations = json.loads(
        (INTEGRATION_DIR / "translations" / "da.json").read_text(encoding="utf-8")
    )

    def _collect_keys(obj: Any, prefix: str = "") -> set[str]:
        keys: set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}.{k}" if prefix else k
                keys.add(path)
                keys |= _collect_keys(v, path)
        return keys

    assert _collect_keys(da_translations) == _collect_keys(translations)
    assert "{api_url}" in da_translations["config"]["step"]["user"]["description"]


def _install_minimal_ha_stubs() -> dict[str, types.ModuleType | None]:
    """Install tiny Home Assistant stubs so coordinator & diagnostics can be tested."""
    saved: dict[str, types.ModuleType | None] = {}

    def _register(name: str, attrs: dict[str, Any] | None = None) -> types.ModuleType:
        if name not in saved:
            saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        if attrs:
            for key, val in attrs.items():
                setattr(mod, key, val)
        sys.modules[name] = mod
        return mod

    class _UpdateFailed(Exception):
        pass

    class _ConfigEntryAuthFailed(Exception):
        pass

    class _ConfigEntryNotReady(Exception):
        pass

    class _DataUpdateCoordinator:
        def __class_getitem__(cls, _item: Any) -> type[Any]:
            return cls

        def __init__(
            self,
            hass: Any,
            logger: Any,
            *,
            config_entry: Any = None,
            name: str = "",
            update_interval: timedelta | None = None,
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.config_entry = config_entry
            self.name = name
            self.update_interval = update_interval
            self.data: Any = None
            self.last_update_success = True

        def async_set_updated_data(self, data: Any) -> None:
            self.data = data

        def async_update_listeners(self) -> None:
            return None

    def _redact(data: Any, to_redact: set[str]) -> Any:
        if isinstance(data, dict):
            return {
                k: "**REDACTED**" if k in to_redact else _redact(v, to_redact)
                for k, v in data.items()
            }
        if isinstance(data, list):
            return [_redact(item, to_redact) for item in data]
        return data

    _register("homeassistant")
    class _UnitOfEnergy:
        KILO_WATT_HOUR = "kWh"

    _register(
        "homeassistant.const",
        {"CONF_API_KEY": "api_key", "UnitOfEnergy": _UnitOfEnergy},
    )
    _register(
        "homeassistant.core",
        {"HomeAssistant": object, "callback": lambda fn: fn},
    )
    _register(
        "homeassistant.exceptions",
        {
            "ConfigEntryAuthFailed": _ConfigEntryAuthFailed,
            "ConfigEntryNotReady": _ConfigEntryNotReady,
        },
    )
    _register(
        "homeassistant.helpers",
        {},
    )
    _register(
        "homeassistant.helpers.update_coordinator",
        {
            "DataUpdateCoordinator": _DataUpdateCoordinator,
            "UpdateFailed": _UpdateFailed,
        },
    )
    _register("homeassistant.components")
    _register(
        "homeassistant.components.diagnostics",
        {"async_redact_data": _redact},
    )
    _register("homeassistant.util")
    _register(
        "homeassistant.util.dt",
        {
            "DEFAULT_TIME_ZONE": ZoneInfo("UTC"),
            "get_time_zone": lambda tz: ZoneInfo(tz),
            "now": lambda tz=None: datetime.now(tz or ZoneInfo("UTC")),
            "utcnow": lambda: datetime.now(ZoneInfo("UTC")),
        },
    )
    return saved


def _restore_modules(saved: dict[str, types.ModuleType | None]) -> None:
    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig
    for name in list(sys.modules):
        if name.startswith("osmund_stub_pkg"):
            sys.modules.pop(name, None)


def _load_pkg_modules(*module_names: str) -> dict[str, types.ModuleType]:
    """Load integration modules under an isolated package name."""
    pkg_name = "osmund_stub_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(INTEGRATION_DIR)]
    # Minimal placeholder for `from . import OsmundConfigEntry`
    pkg.OsmundConfigEntry = Any  # type: ignore[attr-defined]
    sys.modules[pkg_name] = pkg

    loaded: dict[str, types.ModuleType] = {}
    for mod_name in module_names:
        full_name = f"{pkg_name}.{mod_name}"
        spec = importlib.util.spec_from_file_location(
            full_name, INTEGRATION_DIR / f"{mod_name}.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = mod
        spec.loader.exec_module(mod)
        loaded[mod_name] = mod
    return loaded


async def test_diagnostics_redacts_api_key_and_optional_labels() -> None:
    """Verify diagnostics always redacts api_key and honors label redaction option."""
    saved = _install_minimal_ha_stubs()
    try:
        mods = _load_pkg_modules(
            "const", "api", "statistics", "coordinator", "diagnostics"
        )
        api = mods["api"]
        coord = mods["coordinator"]
        diag = mods["diagnostics"]

        loc = api.Location.from_dict(
            {
                "id": "loc-1",
                "label": "My Secret Street 42",
                "priceArea": "DK1",
                "state": "active",
                "plan": "osmund_rolig",
                "apiAccess": "open",
                "plus": {"status": "included", "trialEndsOn": None, "endsOn": None},
                "consumptionThrough": "2026-11-20T00:00:00+01:00",
                "meterResolution": "PT15M",
                "ratedThrough": "2026-11-19",
            }
        )
        key_details = api.KeyDetails(
            name="Home Assistant",
            prefix="osm_12345678",
            scopes=("prices:read", "consumption:read", "costs:read", "labels:read"),
            expires_at="2027-11-16T09:12:00+01:00",
        )

        @dataclass
        class _FakeClient:
            last_rate_limit: Any = api.RateLimitInfo(limit=60, remaining=58, reset=40)

        @dataclass
        class _FakeCoordinator:
            data: Any
            last_update_success: bool = True

        @dataclass
        class _FakeRuntime:
            client: Any
            key_details: Any
            rate_limit_gate: Any
            account_coordinator: Any
            price_coordinator: Any

        @dataclass
        class _FakeEntry:
            title: str
            data: dict[str, Any]
            options: dict[str, Any]
            runtime_data: Any

        secret_key = "osm_" + "do_not_expose_this_secret"
        runtime = _FakeRuntime(
            client=_FakeClient(),
            key_details=key_details,
            rate_limit_gate=coord.RateLimitGate(),
            account_coordinator=_FakeCoordinator(
                data=coord.AccountData(
                    key_details=key_details, locations={"loc-1": loc}
                )
            ),
            price_coordinator=_FakeCoordinator(data={}),
        )

        # Redact labels = True (default)
        entry_redacted = _FakeEntry(
            title="Osmund Energi (HA)",
            data={"api_key": secret_key},
            options={"redact_location_labels": True, "price_resolution": "auto"},
            runtime_data=runtime,
        )
        out_redacted = await diag.async_get_config_entry_diagnostics(
            None, entry_redacted
        )
        assert out_redacted["entry"]["data"]["api_key"] == "**REDACTED**"
        assert out_redacted["locations"][0]["label"] == "**REDACTED**"
        assert secret_key not in json.dumps(out_redacted)
        assert "My Secret Street 42" not in json.dumps(out_redacted)

        # Redact labels = False
        entry_visible = _FakeEntry(
            title="Osmund Energi (HA)",
            data={"api_key": secret_key},
            options={"redact_location_labels": False, "price_resolution": "auto"},
            runtime_data=runtime,
        )
        out_visible = await diag.async_get_config_entry_diagnostics(None, entry_visible)
        assert out_visible["entry"]["data"]["api_key"] == "**REDACTED**"
        assert out_visible["locations"][0]["label"] == "My Secret Street 42"
        assert secret_key not in json.dumps(out_visible)
    finally:
        _restore_modules(saved)


async def test_price_coordinator_cache_and_zero_call_service() -> None:
    """Verify price coordinator caches days and serves get_prices without API calls."""
    saved = _install_minimal_ha_stubs()
    try:
        mods = _load_pkg_modules("const", "api", "statistics", "coordinator")
        api = mods["api"]
        coord = mods["coordinator"]

        # Fix Danish time to 14:00 so both today & tomorrow are polled once
        fixed_dk = datetime(2026, 11, 21, 14, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))
        coord.danish_now = lambda: fixed_dk

        loc = api.Location.from_dict(
            {
                "id": "loc-1",
                "label": "Hjem",
                "priceArea": "DK1",
                "state": "active",
                "plan": "osmund_rolig",
                "apiAccess": "open",
                "plus": {"status": "included", "trialEndsOn": None, "endsOn": None},
                "consumptionThrough": None,
                "meterResolution": "PT15M",
                "ratedThrough": None,
            }
        )
        key_details = api.KeyDetails(
            name="HA",
            prefix="osm_abc",
            scopes=("prices:read",),
            expires_at="2027-11-16T09:12:00+01:00",
        )

        class _FakeClient:
            def __init__(self) -> None:
                self.price_calls: list[tuple[str, str | None, str]] = []

            async def async_get_prices(
                self,
                location_id: str,
                *,
                date: str | None = None,
                resolution: str = "PT1H",
            ) -> Any:
                self.price_calls.append((location_id, date, resolution))
                return api.PriceDay.from_dict(
                    {
                        "locationId": location_id,
                        "date": date or "2026-11-21",
                        "resolution": resolution,
                        "priceArea": "DK1",
                        "currency": "DKK",
                        "unit": "kr/kWh",
                        "complete": True,
                        "reason": None,
                        "intervals": [
                            {
                                "start": f"{date}T00:00:00+01:00",
                                "end": f"{date}T00:15:00+01:00",
                                "spot": 0.4,
                                "totalExVat": 0.8,
                                "totalIncVat": 1.0,
                                "components": [],
                            }
                        ],
                    }
                )

        @dataclass
        class _FakeAcctCoord:
            key_details: Any
            open_locations: list[Any]

        @dataclass
        class _FakeEntry:
            options: dict[str, Any]

        client = _FakeClient()
        price_coord = coord.OsmundPriceCoordinator(
            hass=None,
            config_entry=_FakeEntry(options={"price_resolution": "auto"}),
            client=client,
            account_coordinator=_FakeAcctCoord(
                key_details=key_details, open_locations=[loc]
            ),
            rate_limit_gate=coord.RateLimitGate(),
        )

        # First poll: fetches today + tomorrow in PT15M (auto -> meterResolution)
        data = await price_coord._async_update_data()
        price_coord.data = data
        assert len(client.price_calls) == 2
        assert client.price_calls == [
            ("loc-1", "2026-11-21", "PT15M"),
            ("loc-1", "2026-11-22", "PT15M"),
        ]

        # Second poll on same afternoon: 0 API calls (both days complete!)
        await price_coord._async_update_data()
        assert len(client.price_calls) == 2

        # Service call for today & tomorrow at configured resolution: 0 API calls!
        res, days = await price_coord.async_get_prices_for_service(loc, "both")
        assert res == "PT15M"
        assert set(days) == {"2026-11-21", "2026-11-22"}
        assert len(client.price_calls) == 2

        # Service call requesting alternate resolution (PT1H): 1 API call, then cached!
        res_h, days_h = await price_coord.async_get_prices_for_service(
            loc, "today", requested_resolution="PT1H"
        )
        assert res_h == "PT1H"
        assert "2026-11-21" in days_h
        assert len(client.price_calls) == 3

        await price_coord.async_get_prices_for_service(
            loc, "today", requested_resolution="PT1H"
        )
        assert len(client.price_calls) == 3

        # Midnight rollover promotes tomorrow -> today with 0 API calls
        midnight_dk = datetime(
            2026, 11, 22, 0, 0, 1, tzinfo=ZoneInfo("Europe/Copenhagen")
        )
        assert price_coord.promote_tomorrow_if_midnight(midnight_dk) is True
        assert price_coord.data["loc-1"].today.date == "2026-11-22"
        assert price_coord.data["loc-1"].tomorrow is None
        assert len(client.price_calls) == 3
    finally:
        _restore_modules(saved)


async def test_account_coordinator_cursor_gating() -> None:
    """Verify consumption/costs only re-fetch when cursors advance or day changes."""
    saved = _install_minimal_ha_stubs()
    try:
        mods = _load_pkg_modules("const", "api", "statistics", "coordinator")
        api = mods["api"]
        coord = mods["coordinator"]

        fixed_dk = datetime(2026, 11, 21, 10, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))
        coord.danish_now = lambda: fixed_dk

        class _FakeClient:
            def __init__(self) -> None:
                self.consumption_through = "2026-11-20T00:00:00+01:00"
                self.rated_through = "2026-11-19"
                self.counts = {
                    "locations": 0,
                    "consumption": 0,
                    "standby": 0,
                    "rated_cost": 0,
                    "invoices": 0,
                }

            async def async_list_locations(self) -> list[Any]:
                self.counts["locations"] += 1
                return [
                    api.Location.from_dict(
                        {
                            "id": "loc-1",
                            "label": "Hjem",
                            "priceArea": "DK1",
                            "state": "active",
                            "plan": "osmund_rolig",
                            "apiAccess": "open",
                            "plus": {
                                "status": "included",
                                "trialEndsOn": None,
                                "endsOn": None,
                            },
                            "consumptionThrough": self.consumption_through,
                            "meterResolution": "PT15M",
                            "ratedThrough": self.rated_through,
                        }
                    )
                ]

            async def async_get_consumption(self, *args: Any, **kwargs: Any) -> Any:
                self.counts["consumption"] += 1
                return api.Consumption.from_dict(
                    {
                        "locationId": "loc-1",
                        "resolution": "P1D",
                        "from": "2026-11-01",
                        "to": "2026-11-21",
                        "unit": "kWh",
                        "totalKwh": 100.0,
                        "values": [],
                    }
                )

            async def async_get_standby(self, *args: Any, **kwargs: Any) -> Any:
                self.counts["standby"] += 1
                return api.Standby.from_dict(
                    {"locationId": "loc-1", "unit": "kW", "months": []}
                )

            async def async_get_rated_cost(self, *args: Any, **kwargs: Any) -> Any:
                self.counts["rated_cost"] += 1
                return api.RatedCost.from_dict(
                    {
                        "locationId": "loc-1",
                        "month": "2026-11",
                        "currency": "DKK",
                        "complete": False,
                        "missingDays": [],
                        "daysInMonth": 30,
                        "daysWithData": 1,
                        "totalKwh": 10.0,
                        "total": {"exVat": 10.0, "incVat": 12.5},
                        "subscriptions": {"exVat": 2.0, "incVat": 2.5},
                        "totalWithSubscriptions": {"exVat": 12.0, "incVat": 15.0},
                        "subscriptionsComplete": True,
                        "days": [
                            {
                                "date": "2026-11-19",
                                "kwh": 10.0,
                                "exVat": 10.0,
                                "incVat": 12.5,
                                "subscriptions": {"exVat": 2.0, "incVat": 2.5},
                                "totalWithSubscriptions": {
                                    "exVat": 12.0,
                                    "incVat": 15.0,
                                },
                                "components": [],
                            }
                        ],
                    }
                )

            async def async_list_invoices(self, *args: Any, **kwargs: Any) -> Any:
                self.counts["invoices"] += 1
                return api.InvoiceList.from_dict(
                    {"page": 1, "perPage": 25, "hasMore": False, "invoices": []}
                )

        key_details = api.KeyDetails(
            name="HA",
            prefix="osm_abc",
            scopes=("prices:read", "consumption:read", "costs:read"),
            expires_at="2027-11-16T09:12:00+01:00",
        )
        client = _FakeClient()
        acct = coord.OsmundAccountCoordinator(
            hass=None,
            config_entry=None,
            client=client,
            key_details=key_details,
            rate_limit_gate=coord.RateLimitGate(),
        )

        # First poll: fetches everything once
        await acct._async_update_data()
        assert client.counts == {
            "locations": 1,
            "consumption": 1,
            "standby": 1,
            "rated_cost": 1,
            "invoices": 1,
        }

        # Second poll (unchanged cursors, same day): ONLY /locations called!
        await acct._async_update_data()
        assert client.counts == {
            "locations": 2,
            "consumption": 1,
            "standby": 1,
            "rated_cost": 1,
            "invoices": 1,
        }

        # Cursor moves for consumption only: ONLY /locations + /consumption called!
        client.consumption_through = "2026-11-20T12:00:00+01:00"
        await acct._async_update_data()
        assert client.counts == {
            "locations": 3,
            "consumption": 2,
            "standby": 1,
            "rated_cost": 1,
            "invoices": 1,
        }
    finally:
        _restore_modules(saved)


def test_statistics_slug_window_and_cumulative_sums() -> None:
    """Verify Energy Dashboard external statistics helpers, DST, and anchor sums."""
    saved = _install_minimal_ha_stubs()
    try:
        mods = _load_pkg_modules("const", "api", "statistics")
        api = mods["api"]
        stats = mods["statistics"]
        cph = ZoneInfo("Europe/Copenhagen")

        loc_id = "01a1faca-3a2e-7407-a556-559f006e2f31"
        assert (
            stats.consumption_statistic_id(loc_id)
            == "osmund_energi:01a1faca_3a2e_7407_a556_559f006e2f31_energy_consumption"
        )
        assert (
            stats.cost_statistic_id(loc_id)
            == "osmund_energi:01a1faca_3a2e_7407_a556_559f006e2f31_energy_cost"
        )

        # Initial backfill window (30 days) vs incremental 5-day lookback
        assert stats.compute_hourly_consumption_window(
            date(2026, 11, 22), None, cph
        ) == (date(2026, 10, 23), date(2026, 11, 22))
        assert stats.compute_hourly_consumption_window(
            date(2026, 11, 22),
            datetime(2026, 11, 20, 22, 0, tzinfo=UTC),
            cph,
        ) == (date(2026, 11, 15), date(2026, 11, 22))

        # DST fall-back day (2026-10-25): 02:00+02:00 and 02:00+01:00 are distinct UTC
        values = [
            api.ConsumptionValue.from_dict(
                {
                    "start": "2026-10-25T01:00:00+02:00",
                    "end": "2026-10-25T02:00:00+02:00",
                    "kwh": 1.0,
                }
            ),
            api.ConsumptionValue.from_dict(
                {
                    "start": "2026-10-25T02:00:00+02:00",
                    "end": "2026-10-25T02:00:00+01:00",
                    "kwh": 1.5,
                }
            ),
            api.ConsumptionValue.from_dict(
                {
                    "start": "2026-10-25T02:00:00+01:00",
                    "end": "2026-10-25T03:00:00+01:00",
                    "kwh": 2.0,
                }
            ),
        ]
        # Anchor at 23:00Z excludes the first reading (01:00+02:00 == 23:00Z)
        rows = stats.build_consumption_statistics(
            values,
            anchor_start_utc=datetime(2026, 10, 24, 23, 0, tzinfo=UTC),
            anchor_sum=10.0,
        )
        assert len(rows) == 2
        assert rows[0]["start"] == datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
        assert rows[0]["sum"] == 11.5
        assert rows[1]["start"] == datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
        assert rows[1]["sum"] == 13.5

        # Daily rated cost statistics placed at Danish midnight -> UTC top-of-hour
        cost_days = [
            api.RatedCostDay.from_dict(
                {
                    "date": "2026-11-21",
                    "kwh": 12.0,
                    "exVat": 16.0,
                    "incVat": 20.0,
                    "subscriptions": {"exVat": 3.0, "incVat": 3.75},
                    "totalWithSubscriptions": {"exVat": 19.0, "incVat": 23.75},
                    "components": [],
                }
            ),
            api.RatedCostDay.from_dict(
                {
                    "date": "2026-11-22",
                    "kwh": 10.0,
                    "exVat": 14.0,
                    "incVat": 17.5,
                    "subscriptions": {"exVat": 3.0, "incVat": 3.75},
                    "totalWithSubscriptions": {"exVat": 17.0, "incVat": 21.25},
                    "components": [],
                }
            ),
        ]
        cost_rows = stats.build_cost_statistics(
            cost_days,
            anchor_start_utc=None,
            anchor_sum=0.0,
            tz=cph,
        )
        assert len(cost_rows) == 2
        assert cost_rows[0]["start"] == datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
        assert cost_rows[0]["state"] == 23.75
        assert cost_rows[0]["sum"] == 23.75
        assert cost_rows[1]["start"] == datetime(2026, 11, 21, 23, 0, tzinfo=UTC)
        assert cost_rows[1]["sum"] == 45.0
    finally:
        _restore_modules(saved)


def test_brand_icons_present_and_valid_png() -> None:
    """Verify HACS brand icons exist with exact 256x256 and 512x512 PNG headers."""
    import struct

    for filename, expected_size in (("icon.png", 256), ("icon@2x.png", 512)):
        path = INTEGRATION_DIR / "brand" / filename
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (expected_size, expected_size)
