"""External long-term statistics import for the Home Assistant Energy Dashboard.

Osmund Energi meter readings arrive 14–43 hours after each hour, and rated costs
follow them. Importing historical hourly consumption (`PT1H`) and daily rated
costs via `async_add_external_statistics` places readings at their exact
historical timestamps in the Energy Dashboard while staying idempotent across
late-arriving readings and re-rated days.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import ConsumptionValue, Location, RatedCostDay, parse_iso_date
from .const import CURRENCY_DKK, DANISH_TZ_NAME, DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )

_LOGGER = logging.getLogger(__name__)

# Osmund API allows at most 31 days of PT1H consumption per request.
INITIAL_BACKFILL_DAYS = 30
MAX_HOURLY_WINDOW_DAYS = 30
STATISTICS_LOOKBACK_DAYS = 5
ANCHOR_SEARCH_DAYS = 90


def location_stat_slug(location_id: str) -> str:
    """Return a valid HA external statistic slug (lowercase, underscores only)."""
    return "".join(
        ch if ch.isalnum() else "_" for ch in location_id.lower()
    ).strip("_")


def consumption_statistic_id(location_id: str) -> str:
    """Return the external Energy Dashboard statistic_id for hourly kWh."""
    return f"{DOMAIN}:{location_stat_slug(location_id)}_energy_consumption"


def cost_statistic_id(location_id: str) -> str:
    """Return the external Energy Dashboard statistic_id for rated DKK costs."""
    return f"{DOMAIN}:{location_stat_slug(location_id)}_energy_cost"


def compute_hourly_consumption_window(
    today_dk: date,
    last_stat_utc: datetime | None,
    tz: tzinfo,
) -> tuple[date, date]:
    """Compute `(from_date, to_date)` in Danish dates for `PT1H` consumption fetch."""
    if last_stat_utc is None:
        return today_dk - timedelta(days=INITIAL_BACKFILL_DAYS), today_dk

    last_stat_dk_date = last_stat_utc.astimezone(tz).date()
    desired_start = min(last_stat_dk_date, today_dk) - timedelta(
        days=STATISTICS_LOOKBACK_DAYS
    )
    earliest_allowed = today_dk - timedelta(days=MAX_HOURLY_WINDOW_DAYS)
    return max(desired_start, earliest_allowed), today_dk


def build_consumption_statistics(
    values: Sequence[ConsumptionValue],
    *,
    anchor_start_utc: datetime | None,
    anchor_sum: float,
) -> list[dict[str, Any]]:
    """Build cumulative hour-aligned StatisticData dicts from hourly kWh values."""
    by_start: dict[datetime, float] = {}
    for item in values:
        start_utc = item.start.astimezone(UTC)
        if start_utc.minute != 0 or start_utc.second != 0 or start_utc.microsecond != 0:
            continue
        if anchor_start_utc is not None and start_utc <= anchor_start_utc:
            continue
        by_start[start_utc] = item.kwh

    running_sum = anchor_sum
    rows: list[dict[str, Any]] = []
    for start_utc in sorted(by_start):
        kwh = by_start[start_utc]
        running_sum = round(running_sum + kwh, 6)
        rows.append({"start": start_utc, "state": kwh, "sum": running_sum})
    return rows


def build_cost_statistics(
    days: Sequence[RatedCostDay],
    *,
    anchor_start_utc: datetime | None,
    anchor_sum: float,
    tz: tzinfo,
) -> list[dict[str, Any]]:
    """Build cumulative hour-aligned StatisticData dicts from daily rated costs."""
    by_start: dict[datetime, float] = {}
    for day in days:
        parsed_date = parse_iso_date(day.date)
        if parsed_date is None:
            continue
        # Place each rated day at Danish midnight (exact top-of-hour in UTC)
        start_utc = datetime.combine(parsed_date, time.min, tzinfo=tz).astimezone(UTC)
        if anchor_start_utc is not None and start_utc <= anchor_start_utc:
            continue
        by_start[start_utc] = day.total_with_subscriptions.inc_vat

    running_sum = anchor_sum
    rows: list[dict[str, Any]] = []
    for start_utc in sorted(by_start):
        cost = by_start[start_utc]
        running_sum = round(running_sum + cost, 4)
        rows.append({"start": start_utc, "state": cost, "sum": running_sum})
    return rows


def _make_metadata(
    *,
    statistic_id: str,
    name: str,
    unit_of_measurement: str,
    unit_class: str | None,
) -> "StatisticMetaData":
    """Construct StatisticMetaData compatible with HA 2025.1+ and 2026.x."""
    try:
        from homeassistant.components.recorder.models import (
            StatisticMeanType,
            StatisticMetaData,
        )

        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=unit_class,
            unit_of_measurement=unit_of_measurement,
        )
    except (ImportError, TypeError):
        from homeassistant.components.recorder.models import StatisticMetaData

        return StatisticMetaData(  # type: ignore[call-arg]
            has_mean=False,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=unit_of_measurement,
        )


def _stat_start_to_utc(raw_start: float | int | datetime) -> datetime:
    """Convert a recorder statistic `start` value (unix float or datetime) to UTC."""
    if isinstance(raw_start, datetime):
        return raw_start.astimezone(UTC)
    return dt_util.utc_from_timestamp(float(raw_start))


async def async_get_last_statistic_utc(
    hass: HomeAssistant,
    statistic_id: str,
) -> datetime | None:
    """Return the UTC start timestamp of the latest recorded statistic, or None."""
    if "recorder" not in hass.config.components:
        return None

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import get_last_statistics

    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    entries = last_stats.get(statistic_id)
    if not entries:
        return None
    return _stat_start_to_utc(entries[0]["start"])


async def _async_find_anchor_before(
    hass: HomeAssistant,
    statistic_id: str,
    window_start_utc: datetime,
) -> tuple[datetime | None, float]:
    """Find the latest stored `(start_utc, sum)` strictly before `window_start_utc`."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    search_start = window_start_utc - timedelta(days=ANCHOR_SEARCH_DAYS)
    stats_map = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        search_start,
        window_start_utc,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    rows = stats_map.get(statistic_id) or []

    # Fallback if there was a >90-day gap prior to window_start_utc
    if not rows:
        stats_map = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            datetime(2000, 1, 1, tzinfo=UTC),
            window_start_utc,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats_map.get(statistic_id) or []

    anchor_start: datetime | None = None
    anchor_sum = 0.0
    for row in rows:
        row_start = _stat_start_to_utc(row["start"])
        if row_start < window_start_utc and row.get("sum") is not None:
            anchor_start = row_start
            anchor_sum = float(row["sum"])
    return anchor_start, anchor_sum


async def async_import_consumption_statistics(
    hass: HomeAssistant,
    location: Location,
    values: Sequence[ConsumptionValue],
) -> int:
    """Import hourly kWh readings into Home Assistant external statistics."""
    if not values or "recorder" not in hass.config.components:
        return 0

    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    stat_id = consumption_statistic_id(location.id)
    earliest_utc = min(v.start.astimezone(UTC) for v in values)
    anchor_start, anchor_sum = await _async_find_anchor_before(
        hass, stat_id, earliest_utc
    )

    rows = build_consumption_statistics(
        values,
        anchor_start_utc=anchor_start,
        anchor_sum=anchor_sum,
    )
    if not rows:
        return 0

    metadata = _make_metadata(
        statistic_id=stat_id,
        name=f"{location.display_name} electricity consumption",
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        unit_class="energy",
    )
    stats: list[StatisticData] = rows  # type: ignore[assignment]
    async_add_external_statistics(hass, metadata, stats)
    _LOGGER.debug(
        "Imported %d hourly consumption statistics for %s (%s)",
        len(rows),
        location.id,
        stat_id,
    )
    return len(rows)


async def async_import_cost_statistics(
    hass: HomeAssistant,
    location: Location,
    days: Sequence[RatedCostDay],
) -> int:
    """Import daily rated DKK costs into Home Assistant external statistics."""
    if not days or "recorder" not in hass.config.components:
        return 0

    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    tz = dt_util.get_time_zone(DANISH_TZ_NAME) or dt_util.DEFAULT_TIME_ZONE
    parsed_starts = [
        datetime.combine(parsed, time.min, tzinfo=tz).astimezone(UTC)
        for day in days
        if (parsed := parse_iso_date(day.date)) is not None
    ]
    if not parsed_starts:
        return 0

    stat_id = cost_statistic_id(location.id)
    earliest_utc = min(parsed_starts)
    anchor_start, anchor_sum = await _async_find_anchor_before(
        hass, stat_id, earliest_utc
    )

    rows = build_cost_statistics(
        days,
        anchor_start_utc=anchor_start,
        anchor_sum=anchor_sum,
        tz=tz,
    )
    if not rows:
        return 0

    metadata = _make_metadata(
        statistic_id=stat_id,
        name=f"{location.display_name} electricity cost",
        unit_of_measurement=CURRENCY_DKK,
        unit_class=None,
    )
    stats: list[StatisticData] = rows  # type: ignore[assignment]
    async_add_external_statistics(hass, metadata, stats)
    _LOGGER.debug(
        "Imported %d daily cost statistics for %s (%s)",
        len(rows),
        location.id,
        stat_id,
    )
    return len(rows)
