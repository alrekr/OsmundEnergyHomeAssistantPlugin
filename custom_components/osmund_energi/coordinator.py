"""DataUpdateCoordinators for the Osmund Energi integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Consumption,
    InvoiceList,
    KeyDetails,
    Location,
    OsmundAccessError,
    OsmundApiClient,
    OsmundApiError,
    OsmundAuthError,
    OsmundConnectionError,
    OsmundRateLimitError,
    OsmundScopeError,
    PriceDay,
    RatedCost,
    RatedCostDay,
    Standby,
)
from .const import (
    CONF_PRICE_RESOLUTION,
    DANISH_TZ_NAME,
    DAY_BOTH,
    DAY_TODAY,
    DAY_TOMORROW,
    DEFAULT_PRICE_RESOLUTION,
    DOMAIN,
    SCOPE_CONSUMPTION_READ,
    SCOPE_COSTS_READ,
    SCOPE_PRICES_READ,
)

if TYPE_CHECKING:
    from . import OsmundConfigEntry

_LOGGER = logging.getLogger(__name__)

ACCOUNT_UPDATE_INTERVAL = timedelta(minutes=30)
PRICE_UPDATE_INTERVAL = timedelta(minutes=30)
INVOICE_REFRESH_INTERVAL = timedelta(hours=6)
LATE_READINGS_LOOKBACK_DAYS = 5
TOMORROW_POLL_START_HOUR = 12
TOMORROW_POLL_START_MINUTE = 45
MIN_COOLDOWN_SECONDS = 15
INCOMPLETE_SERVICE_CACHE_TTL = timedelta(minutes=15)


def danish_now() -> datetime:
    """Return the current time in Danish timezone (Europe/Copenhagen)."""
    tz = dt_util.get_time_zone(DANISH_TZ_NAME) or dt_util.DEFAULT_TIME_ZONE
    return dt_util.now(tz)


@dataclass(slots=True)
class RateLimitGate:
    """Shared cooldown gate so coordinators respect Retry-After headers together."""

    cooldown_until: datetime | None = None

    def is_cooling_down(self, now: datetime) -> bool:
        """Return True if requests should wait due to an active Retry-After."""
        return self.cooldown_until is not None and now < self.cooldown_until

    def remaining_seconds(self, now: datetime) -> int:
        """Return remaining cooldown seconds, or 0 if not cooling down."""
        if self.cooldown_until is None or now >= self.cooldown_until:
            return 0
        return max(1, int((self.cooldown_until - now).total_seconds()))

    def trigger(self, now: datetime, retry_after: int | None) -> int:
        """Set a cooldown window based on Retry-After."""
        wait = max(retry_after or 60, MIN_COOLDOWN_SECONDS)
        candidate = now + timedelta(seconds=wait)
        if self.cooldown_until is None or candidate > self.cooldown_until:
            self.cooldown_until = candidate
        return wait


@dataclass(slots=True)
class LocationRatedCostData:
    """Current month rated cost plus the latest rated day (handles month rollovers)."""

    current_month: RatedCost
    latest_day: RatedCostDay | None


@dataclass(slots=True)
class AccountData:
    """Snapshot managed by OsmundAccountCoordinator."""

    key_details: KeyDetails
    locations: dict[str, Location] = field(default_factory=dict)
    consumption: dict[str, Consumption] = field(default_factory=dict)
    standby: dict[str, Standby] = field(default_factory=dict)
    rated_cost: dict[str, LocationRatedCostData] = field(default_factory=dict)
    invoices: InvoiceList | None = None

    @property
    def open_locations(self) -> list[Location]:
        """Return all locations with active API access (`apiAccess == 'open'`)."""
        return [loc for loc in self.locations.values() if loc.has_api_access]


@dataclass(slots=True)
class LocationPrices:
    """Cached electricity prices (today & tomorrow) for a single location."""

    resolution: str
    today: PriceDay | None = None
    tomorrow: PriceDay | None = None


class OsmundAccountCoordinator(DataUpdateCoordinator[AccountData]):
    """Poll /locations and cursor-gated consumption, standby, costs & invoices."""

    config_entry: OsmundConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: OsmundConfigEntry,
        client: OsmundApiClient,
        key_details: KeyDetails,
        rate_limit_gate: RateLimitGate,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_account",
            update_interval=ACCOUNT_UPDATE_INTERVAL,
        )
        self.client = client
        self.key_details = key_details
        self.rate_limit_gate = rate_limit_gate

        # Cursor and freshness caches per location
        self._consumption_cache: dict[str, Consumption] = {}
        self._last_consumption_through: dict[str, str | None] = {}
        self._last_consumption_daily_date: dict[str, str] = {}

        self._standby_cache: dict[str, Standby] = {}
        self._last_standby_date: dict[str, str] = {}

        self._rated_cost_cache: dict[str, LocationRatedCostData] = {}
        self._last_rated_through: dict[str, str | None] = {}
        self._last_rated_daily_date: dict[str, str] = {}

        self._invoices_cache: InvoiceList | None = None
        self._last_invoices_refresh: datetime | None = None

        self._warned_missing_scopes: set[str] = set()

    @property
    def open_locations(self) -> list[Location]:
        """Return currently known open locations."""
        if self.data is None:
            return []
        return self.data.open_locations

    async def _async_update_data(self) -> AccountData:
        """Fetch locations and refresh metered data when cursors/cadence warrant."""
        now_utc = dt_util.utcnow()
        if self.rate_limit_gate.is_cooling_down(now_utc):
            if self.data is not None:
                _LOGGER.debug(
                    "Skipping account poll during rate-limit cooldown (%ss remaining)",
                    self.rate_limit_gate.remaining_seconds(now_utc),
                )
                return self.data
            raise UpdateFailed(
                "Osmund Energi API rate limit cooldown active "
                f"({self.rate_limit_gate.remaining_seconds(now_utc)}s remaining)"
            )

        now_dk = danish_now()
        today_dk = now_dk.date()
        today_str = today_dk.isoformat()
        current_month_str = today_str[:7]

        try:
            locations_list = await self.client.async_list_locations()
        except OsmundAuthError as err:
            raise ConfigEntryAuthFailed(
                "Osmund Energi API key is invalid, expired, or revoked"
            ) from err
        except (OsmundRateLimitError, OsmundConnectionError) as err:
            wait = self.rate_limit_gate.trigger(now_utc, err.retry_after)
            raise UpdateFailed(
                f"Osmund Energi API temporarily unavailable (retry in {wait}s): {err}"
            ) from err
        except OsmundApiError as err:
            raise UpdateFailed(
                f"Error fetching Osmund Energi locations: {err}"
            ) from err

        locations = {loc.id: loc for loc in locations_list}
        open_locations = [loc for loc in locations_list if loc.has_api_access]

        for loc in open_locations:
            if self.rate_limit_gate.is_cooling_down(dt_util.utcnow()):
                break

            await self._async_refresh_consumption(loc, today_dk, today_str)
            await self._async_refresh_standby(loc, today_str)
            await self._async_refresh_rated_cost(loc, current_month_str, today_str)

        if open_locations and not self.rate_limit_gate.is_cooling_down(
            dt_util.utcnow()
        ):
            await self._async_refresh_invoices(now_utc)

        return AccountData(
            key_details=self.key_details,
            locations=locations,
            consumption=dict(self._consumption_cache),
            standby=dict(self._standby_cache),
            rated_cost=dict(self._rated_cost_cache),
            invoices=self._invoices_cache,
        )

    async def _async_refresh_consumption(
        self,
        loc: Location,
        today_dk: date,
        today_str: str,
    ) -> None:
        """Refresh consumption when cursor moves or daily late-readings check is due."""
        if not self.key_details.has_scope(SCOPE_CONSUMPTION_READ):
            return

        loc_id = loc.id
        needs_fetch = (
            loc_id not in self._consumption_cache
            or loc.consumption_through != self._last_consumption_through.get(loc_id)
            or self._last_consumption_daily_date.get(loc_id) != today_str
        )
        if not needs_fetch:
            return

        # Single P1D call covers both the entire current month and Osmund's
        # recommended 5-day late-arriving readings lookback (handles month rollover).
        first_of_month = today_dk.replace(day=1)
        lookback_start = today_dk - timedelta(days=LATE_READINGS_LOOKBACK_DAYS)
        date_from = min(first_of_month, lookback_start).isoformat()

        try:
            consumption = await self.client.async_get_consumption(
                loc_id,
                date_from=date_from,
                date_to=today_str,
                resolution="P1D",
            )
        except OsmundAuthError as err:
            raise ConfigEntryAuthFailed(
                "Osmund Energi API key is invalid, expired, or revoked"
            ) from err
        except (OsmundRateLimitError, OsmundConnectionError) as err:
            self.rate_limit_gate.trigger(dt_util.utcnow(), err.retry_after)
            _LOGGER.warning(
                "Rate-limited/unavailable fetching consumption for %s: %s",
                loc_id,
                err,
            )
            return
        except (OsmundScopeError, OsmundAccessError) as err:
            self._log_scope_or_access_once(f"consumption:{loc_id}", err)
            return
        except OsmundApiError as err:
            _LOGGER.warning("Error fetching consumption for %s: %s", loc_id, err)
            return

        self._consumption_cache[loc_id] = consumption
        self._last_consumption_through[loc_id] = loc.consumption_through
        self._last_consumption_daily_date[loc_id] = today_str

    async def _async_refresh_standby(self, loc: Location, today_str: str) -> None:
        """Refresh monthly standby power once per Danish day."""
        if not self.key_details.has_scope(SCOPE_CONSUMPTION_READ):
            return

        loc_id = loc.id
        if (
            loc_id in self._standby_cache
            and self._last_standby_date.get(loc_id) == today_str
        ):
            return

        try:
            standby = await self.client.async_get_standby(loc_id)
        except OsmundAuthError as err:
            raise ConfigEntryAuthFailed(
                "Osmund Energi API key is invalid, expired, or revoked"
            ) from err
        except (OsmundRateLimitError, OsmundConnectionError) as err:
            self.rate_limit_gate.trigger(dt_util.utcnow(), err.retry_after)
            _LOGGER.warning(
                "Rate-limited/unavailable fetching standby for %s: %s", loc_id, err
            )
            return
        except (OsmundScopeError, OsmundAccessError) as err:
            self._log_scope_or_access_once(f"standby:{loc_id}", err)
            return
        except OsmundApiError as err:
            _LOGGER.warning("Error fetching standby for %s: %s", loc_id, err)
            return

        self._standby_cache[loc_id] = standby
        self._last_standby_date[loc_id] = today_str

    async def _async_refresh_rated_cost(
        self,
        loc: Location,
        current_month_str: str,
        today_str: str,
    ) -> None:
        """Refresh rated cost when ratedThrough advances or daily check is due."""
        if not self.key_details.has_scope(SCOPE_COSTS_READ):
            return

        loc_id = loc.id
        needs_fetch = (
            loc_id not in self._rated_cost_cache
            or loc.rated_through != self._last_rated_through.get(loc_id)
            or self._last_rated_daily_date.get(loc_id) != today_str
        )
        if not needs_fetch:
            return

        try:
            current_month = await self.client.async_get_rated_cost(
                loc_id, month=current_month_str
            )
            latest_day = current_month.latest_day

            # Early in a new month, no current-month days may be rated yet;
            # fall back to the month containing `ratedThrough` for latest_daily_cost.
            if (
                latest_day is None
                and loc.rated_through
                and len(loc.rated_through) >= 7
                and loc.rated_through[:7] != current_month_str
            ):
                prev_month = await self.client.async_get_rated_cost(
                    loc_id, month=loc.rated_through[:7]
                )
                latest_day = prev_month.latest_day
        except OsmundAuthError as err:
            raise ConfigEntryAuthFailed(
                "Osmund Energi API key is invalid, expired, or revoked"
            ) from err
        except (OsmundRateLimitError, OsmundConnectionError) as err:
            self.rate_limit_gate.trigger(dt_util.utcnow(), err.retry_after)
            _LOGGER.warning(
                "Rate-limited/unavailable fetching rated cost for %s: %s", loc_id, err
            )
            return
        except (OsmundScopeError, OsmundAccessError) as err:
            self._log_scope_or_access_once(f"rated_cost:{loc_id}", err)
            return
        except OsmundApiError as err:
            _LOGGER.warning("Error fetching rated cost for %s: %s", loc_id, err)
            return

        self._rated_cost_cache[loc_id] = LocationRatedCostData(
            current_month=current_month,
            latest_day=latest_day,
        )
        self._last_rated_through[loc_id] = loc.rated_through
        self._last_rated_daily_date[loc_id] = today_str

    async def _async_refresh_invoices(self, now_utc: datetime) -> None:
        """Refresh account invoices on startup and every 6 hours."""
        if not self.key_details.has_scope(SCOPE_COSTS_READ):
            return

        if (
            self._invoices_cache is not None
            and self._last_invoices_refresh is not None
            and now_utc - self._last_invoices_refresh < INVOICE_REFRESH_INTERVAL
        ):
            return

        try:
            invoices = await self.client.async_list_invoices(page=1, per_page=25)
        except OsmundAuthError as err:
            raise ConfigEntryAuthFailed(
                "Osmund Energi API key is invalid, expired, or revoked"
            ) from err
        except (OsmundRateLimitError, OsmundConnectionError) as err:
            self.rate_limit_gate.trigger(dt_util.utcnow(), err.retry_after)
            _LOGGER.warning("Rate-limited/unavailable fetching invoices: %s", err)
            return
        except (OsmundScopeError, OsmundAccessError) as err:
            self._log_scope_or_access_once("invoices", err)
            return
        except OsmundApiError as err:
            _LOGGER.warning("Error fetching invoices: %s", err)
            return

        self._invoices_cache = invoices
        self._last_invoices_refresh = now_utc

    def _log_scope_or_access_once(self, key: str, err: OsmundApiError) -> None:
        """Log missing scope or closed API access once to avoid log spam."""
        if key not in self._warned_missing_scopes:
            self._warned_missing_scopes.add(key)
            _LOGGER.warning("Skipping %s due to API access/scope: %s", key, err)


class OsmundPriceCoordinator(DataUpdateCoordinator[dict[str, LocationPrices]]):
    """Fetches & caches today and tomorrow electricity prices with minimal API calls."""

    config_entry: OsmundConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: OsmundConfigEntry,
        client: OsmundApiClient,
        account_coordinator: OsmundAccountCoordinator,
        rate_limit_gate: RateLimitGate,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_prices",
            update_interval=PRICE_UPDATE_INTERVAL,
        )
        self.client = client
        self.account_coordinator = account_coordinator
        self.rate_limit_gate = rate_limit_gate

        self._cache: dict[str, LocationPrices] = {}
        # Cache for alternate-resolution requests from osmund_energi.get_prices
        self._service_cache: dict[tuple[str, str, str], tuple[PriceDay, datetime]] = {}
        self._warned_access: set[str] = set()

    @property
    def configured_resolution(self) -> str:
        """Return configured price resolution ('auto', 'PT1H', or 'PT15M')."""
        return str(
            self.config_entry.options.get(
                CONF_PRICE_RESOLUTION, DEFAULT_PRICE_RESOLUTION
            )
        )

    @callback
    def promote_tomorrow_if_midnight(self, now_dk: datetime | None = None) -> bool:
        """Promote cached tomorrow prices to today right at Danish midnight."""
        today_str = (now_dk or danish_now()).date().isoformat()
        promoted = False
        for loc_prices in self._cache.values():
            if (
                loc_prices.tomorrow is not None
                and loc_prices.tomorrow.date == today_str
            ):
                loc_prices.today = loc_prices.tomorrow
                loc_prices.tomorrow = None
                promoted = True
        if promoted and self.data is not None:
            self.async_set_updated_data(dict(self._cache))
        return promoted

    def _prune_service_cache(self, valid_dates: set[str]) -> None:
        stale = [k for k in self._service_cache if k[1] not in valid_dates]
        for key in stale:
            del self._service_cache[key]

    async def _async_update_data(self) -> dict[str, LocationPrices]:
        """Refresh today/tomorrow prices only when missing or incomplete."""
        if not self.account_coordinator.key_details.has_scope(SCOPE_PRICES_READ):
            return {}

        now_utc = dt_util.utcnow()
        now_dk = danish_now()
        today_dk = now_dk.date()
        today_str = today_dk.isoformat()
        tomorrow_str = (today_dk + timedelta(days=1)).isoformat()

        self._prune_service_cache({today_str, tomorrow_str})

        # Roll over midnight from cache first (0 API calls)
        for loc_prices in self._cache.values():
            if (
                loc_prices.tomorrow is not None
                and loc_prices.tomorrow.date == today_str
            ):
                loc_prices.today = loc_prices.tomorrow
                loc_prices.tomorrow = None

        if self.rate_limit_gate.is_cooling_down(now_utc):
            if self._cache:
                _LOGGER.debug(
                    "Skipping price poll during rate-limit cooldown (%ss remaining)",
                    self.rate_limit_gate.remaining_seconds(now_utc),
                )
                return dict(self._cache)
            raise UpdateFailed(
                "Osmund Energi API rate limit cooldown active "
                f"({self.rate_limit_gate.remaining_seconds(now_utc)}s remaining)"
            )

        configured = self.configured_resolution
        poll_tomorrow_window = (now_dk.hour, now_dk.minute) >= (
            TOMORROW_POLL_START_HOUR,
            TOMORROW_POLL_START_MINUTE,
        )

        for loc in self.account_coordinator.open_locations:
            if self.rate_limit_gate.is_cooling_down(dt_util.utcnow()):
                break

            resolution = loc.resolved_price_resolution(configured)
            existing = self._cache.get(loc.id)
            if existing is None or existing.resolution != resolution:
                existing = LocationPrices(resolution=resolution)
                self._cache[loc.id] = existing

            # Fetch today if absent, stale date, or not yet complete
            if (
                existing.today is None
                or existing.today.date != today_str
                or not existing.today.complete
            ):
                today_prices = await self._async_fetch_day(
                    loc.id, today_str, resolution
                )
                if today_prices is not None:
                    existing.today = today_prices

            # Fetch tomorrow only in the afternoon window and until complete
            if poll_tomorrow_window and (
                existing.tomorrow is None
                or existing.tomorrow.date != tomorrow_str
                or not existing.tomorrow.complete
            ):
                tomorrow_prices = await self._async_fetch_day(
                    loc.id, tomorrow_str, resolution
                )
                if tomorrow_prices is not None:
                    existing.tomorrow = tomorrow_prices

        return dict(self._cache)

    async def _async_fetch_day(
        self,
        location_id: str,
        date_str: str,
        resolution: str,
    ) -> PriceDay | None:
        """Fetch a single PriceDay from the API with proper error classification."""
        try:
            return await self.client.async_get_prices(
                location_id, date=date_str, resolution=resolution
            )
        except OsmundAuthError as err:
            raise ConfigEntryAuthFailed(
                "Osmund Energi API key is invalid, expired, or revoked"
            ) from err
        except (OsmundRateLimitError, OsmundConnectionError) as err:
            wait = self.rate_limit_gate.trigger(dt_util.utcnow(), err.retry_after)
            if self._cache:
                _LOGGER.warning(
                    "Rate-limited/unavailable fetching prices for %s (%s); "
                    "using cache for %ss: %s",
                    location_id,
                    date_str,
                    wait,
                    err,
                )
                return None
            raise UpdateFailed(
                f"Osmund Energi API temporarily unavailable (retry in {wait}s): {err}"
            ) from err
        except (OsmundScopeError, OsmundAccessError) as err:
            if location_id not in self._warned_access:
                self._warned_access.add(location_id)
                _LOGGER.warning(
                    "Skipping prices for %s due to API access/scope: %s",
                    location_id,
                    err,
                )
            return None
        except OsmundApiError as err:
            _LOGGER.warning(
                "Error fetching prices for %s (%s): %s", location_id, date_str, err
            )
            return None

    async def async_get_prices_for_service(
        self,
        location: Location,
        day: str,
        requested_resolution: str | None = None,
    ) -> tuple[str, dict[str, PriceDay]]:
        """Return cached PriceDay(s) for service actions (0 calls when cached)."""
        now_utc = dt_util.utcnow()
        now_dk = danish_now()
        today_dk = now_dk.date()
        today_str = today_dk.isoformat()
        tomorrow_str = (today_dk + timedelta(days=1)).isoformat()

        self._prune_service_cache({today_str, tomorrow_str})

        resolution = (
            requested_resolution
            if requested_resolution is not None
            else location.resolved_price_resolution(self.configured_resolution)
        )

        if day == DAY_TODAY:
            target_dates = [today_str]
        elif day == DAY_TOMORROW:
            target_dates = [tomorrow_str]
        elif day == DAY_BOTH:
            target_dates = [today_str, tomorrow_str]
        else:
            target_dates = [today_str]

        loc_cache = self._cache.get(location.id)
        result: dict[str, PriceDay] = {}

        for target_date in target_dates:
            # 1. Fast path: coordinator's own cache (0 API calls)
            if loc_cache is not None and loc_cache.resolution == resolution:
                if (
                    target_date == today_str
                    and loc_cache.today is not None
                    and loc_cache.today.date == today_str
                ):
                    result[target_date] = loc_cache.today
                    continue
                if (
                    target_date == tomorrow_str
                    and loc_cache.tomorrow is not None
                    and loc_cache.tomorrow.date == tomorrow_str
                    and loc_cache.tomorrow.complete
                ):
                    result[target_date] = loc_cache.tomorrow
                    continue

            # 2. Alternate-resolution / recent service cache (0 API calls)
            cache_key = (location.id, target_date, resolution)
            cached_entry = self._service_cache.get(cache_key)
            if cached_entry is not None:
                cached_day, cached_at = cached_entry
                if cached_day.complete or (
                    now_utc - cached_at < INCOMPLETE_SERVICE_CACHE_TTL
                ):
                    result[target_date] = cached_day
                    continue

            # 3. Single on-demand fetch if not cached and not cooling down
            if self.rate_limit_gate.is_cooling_down(now_utc):
                if cached_entry is not None:
                    result[target_date] = cached_entry[0]
                continue

            fetched = await self.client.async_get_prices(
                location.id, date=target_date, resolution=resolution
            )
            self._service_cache[cache_key] = (fetched, now_utc)
            result[target_date] = fetched

        return resolution, result
