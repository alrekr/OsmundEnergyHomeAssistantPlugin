"""Sensor platform for the Osmund Energi integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OsmundConfigEntry
from .api import (
    Location,
    find_next_interval,
    parse_iso_date,
    parse_iso_datetime,
)
from .const import (
    API_TO_OPTION_RESOLUTION,
    CURRENCY_DKK,
    DOMAIN,
    OPTION_RESOLUTION_HOURLY,
    OPTION_RESOLUTION_QUARTER_HOURLY,
    SCOPE_CONSUMPTION_READ,
    SCOPE_COSTS_READ,
    SCOPE_PRICES_READ,
    UNIT_DKK_PER_KWH,
)
from .coordinator import (
    AccountData,
    LocationPrices,
    OsmundAccountCoordinator,
    OsmundPriceCoordinator,
    danish_now,
)

PLAN_DISPLAY_NAMES = {
    "osmund_rolig": "Osmund Rolig",
    "osmund_aaben": "Osmund Åben",
}

PLUS_STATUS_OPTIONS = ["included", "trial", "active", "none"]
SUPPLY_STATE_OPTIONS = ["active", "upcoming", "pending", "ended"]
METER_RESOLUTION_OPTIONS = [
    OPTION_RESOLUTION_QUARTER_HOURLY,
    OPTION_RESOLUTION_HOURLY,
]


def _format_location_model(location: Location) -> str:
    """Format a human-readable device model from plan and price area."""
    plan = (
        PLAN_DISPLAY_NAMES.get(location.plan, location.plan)
        if location.plan
        else "Electricity Supply"
    )
    if location.price_area:
        return f"{plan} ({location.price_area})"
    return plan


def _location_device_info(location: Location) -> DeviceInfo:
    """Build DeviceInfo for an Osmund Energi supply location."""
    return DeviceInfo(
        identifiers={(DOMAIN, location.id)},
        name=location.display_name,
        manufacturer="Osmund Energi",
        model=_format_location_model(location),
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://osmundenergi.dk/min-side",
    )


def _account_device_info(entry: OsmundConfigEntry) -> DeviceInfo:
    """Build DeviceInfo for the Osmund Energi account."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"account_{entry.entry_id}")},
        name="Osmund Energi",
        manufacturer="Osmund Energi",
        model="Account",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://osmundenergi.dk/min-side#api",
    )


@dataclass(frozen=True, kw_only=True)
class OsmundPriceSensorDescription(SensorEntityDescription):
    """Describe an Osmund Energi electricity price sensor."""

    value_fn: Callable[[LocationPrices, datetime], float | datetime | None]


@dataclass(frozen=True, kw_only=True)
class OsmundLocationSensorDescription(SensorEntityDescription):
    """Describe an Osmund Energi per-location metered/diagnostic sensor."""

    value_fn: Callable[[AccountData, Location], float | str | date | datetime | None]
    attrs_fn: Callable[[AccountData, Location], dict[str, Any] | None] | None = None
    required_scope: str | None = None


@dataclass(frozen=True, kw_only=True)
class OsmundAccountSensorDescription(SensorEntityDescription):
    """Describe an Osmund Energi account-level sensor."""

    value_fn: Callable[[AccountData], float | date | datetime | None]
    attrs_fn: Callable[[AccountData], dict[str, Any] | None] | None = None
    required_scope: str | None = None


def _current_interval(prices: LocationPrices, now: datetime):
    if prices.today is None:
        return None
    return prices.today.interval_at(now)


PRICE_SENSOR_DESCRIPTIONS: tuple[OsmundPriceSensorDescription, ...] = (
    OsmundPriceSensorDescription(
        key="current_price",
        translation_key="current_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, now: (
            iv.total_inc_vat if (iv := _current_interval(p, now)) else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="current_price_ex_vat",
        translation_key="current_price_ex_vat",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda p, now: (
            iv.total_ex_vat if (iv := _current_interval(p, now)) else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="current_spot_price",
        translation_key="current_spot_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, now: (
            iv.spot if (iv := _current_interval(p, now)) else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="current_tariff",
        translation_key="current_tariff",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, now: (
            iv.tariff_ex_vat if (iv := _current_interval(p, now)) else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="next_price",
        translation_key="next_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, now: (
            iv.total_inc_vat
            if (iv := find_next_interval(p.today, p.tomorrow, now))
            else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="today_min_price",
        translation_key="today_min_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, _now: (
            iv.total_inc_vat
            if p.today is not None and (iv := p.today.min_interval()) is not None
            else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="today_min_price_at",
        translation_key="today_min_price_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda p, _now: (
            iv.start
            if p.today is not None and (iv := p.today.min_interval()) is not None
            else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="today_max_price",
        translation_key="today_max_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, _now: (
            iv.total_inc_vat
            if p.today is not None and (iv := p.today.max_interval()) is not None
            else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="today_avg_price",
        translation_key="today_avg_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, _now: (
            p.today.avg_price_inc_vat() if p.today is not None else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="tomorrow_min_price",
        translation_key="tomorrow_min_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, _now: (
            iv.total_inc_vat
            if p.tomorrow is not None and (iv := p.tomorrow.min_interval()) is not None
            else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="tomorrow_max_price",
        translation_key="tomorrow_max_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, _now: (
            iv.total_inc_vat
            if p.tomorrow is not None and (iv := p.tomorrow.max_interval()) is not None
            else None
        ),
    ),
    OsmundPriceSensorDescription(
        key="tomorrow_avg_price",
        translation_key="tomorrow_avg_price",
        native_unit_of_measurement=UNIT_DKK_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda p, _now: (
            p.tomorrow.avg_price_inc_vat() if p.tomorrow is not None else None
        ),
    ),
)


def _latest_daily_consumption_value(data: AccountData, loc: Location) -> float | None:
    consumption = data.consumption.get(loc.id)
    if consumption is None or consumption.latest_value is None:
        return None
    return consumption.latest_value.kwh


def _latest_daily_consumption_attrs(
    data: AccountData, loc: Location
) -> dict[str, Any] | None:
    consumption = data.consumption.get(loc.id)
    if consumption is None or consumption.latest_value is None:
        return None
    return {"date": consumption.latest_value.date_str}


def _current_month_consumption_value(data: AccountData, loc: Location) -> float | None:
    consumption = data.consumption.get(loc.id)
    if consumption is None:
        return None
    month_prefix = danish_now().date().isoformat()[:7]
    return consumption.total_for_month(month_prefix)


def _current_month_consumption_attrs(
    data: AccountData, loc: Location
) -> dict[str, Any] | None:
    if loc.id not in data.consumption:
        return None
    return {"month": danish_now().date().isoformat()[:7]}


def _standby_value(data: AccountData, loc: Location) -> float | None:
    standby = data.standby.get(loc.id)
    if standby is None or standby.latest_month is None:
        return None
    return standby.latest_month.standby_kw


def _standby_attrs(data: AccountData, loc: Location) -> dict[str, Any] | None:
    standby = data.standby.get(loc.id)
    if standby is None or standby.latest_month is None:
        return None
    return {"month": standby.latest_month.month}


def _current_month_cost_attrs(
    data: AccountData, loc: Location
) -> dict[str, Any] | None:
    rated = data.rated_cost.get(loc.id)
    if rated is None:
        return None
    return {
        "month": rated.current_month.month,
        "days_with_data": rated.current_month.days_with_data,
        "complete": rated.current_month.complete,
    }


def _latest_daily_cost_value(data: AccountData, loc: Location) -> float | None:
    rated = data.rated_cost.get(loc.id)
    if rated is None or rated.latest_day is None:
        return None
    return rated.latest_day.total_with_subscriptions.inc_vat


def _latest_daily_cost_attrs(
    data: AccountData, loc: Location
) -> dict[str, Any] | None:
    rated = data.rated_cost.get(loc.id)
    if rated is None or rated.latest_day is None:
        return None
    return {
        "date": rated.latest_day.date,
        "kwh": rated.latest_day.kwh,
    }


LOCATION_SENSOR_DESCRIPTIONS: tuple[OsmundLocationSensorDescription, ...] = (
    OsmundLocationSensorDescription(
        key="latest_daily_consumption",
        translation_key="latest_daily_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        required_scope=SCOPE_CONSUMPTION_READ,
        value_fn=_latest_daily_consumption_value,
        attrs_fn=_latest_daily_consumption_attrs,
    ),
    OsmundLocationSensorDescription(
        key="current_month_consumption",
        translation_key="current_month_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        required_scope=SCOPE_CONSUMPTION_READ,
        value_fn=_current_month_consumption_value,
        attrs_fn=_current_month_consumption_attrs,
    ),
    OsmundLocationSensorDescription(
        key="consumption_through",
        translation_key="consumption_through",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        required_scope=SCOPE_CONSUMPTION_READ,
        value_fn=lambda _data, loc: parse_iso_datetime(loc.consumption_through),
    ),
    OsmundLocationSensorDescription(
        key="standby_power",
        translation_key="standby_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=3,
        required_scope=SCOPE_CONSUMPTION_READ,
        value_fn=_standby_value,
        attrs_fn=_standby_attrs,
    ),
    OsmundLocationSensorDescription(
        key="current_month_cost",
        translation_key="current_month_cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_DKK,
        suggested_display_precision=2,
        required_scope=SCOPE_COSTS_READ,
        value_fn=lambda data, loc: (
            rated.current_month.total_with_subscriptions.inc_vat
            if (rated := data.rated_cost.get(loc.id)) is not None
            else None
        ),
        attrs_fn=_current_month_cost_attrs,
    ),
    OsmundLocationSensorDescription(
        key="current_month_consumption_cost",
        translation_key="current_month_consumption_cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_DKK,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        required_scope=SCOPE_COSTS_READ,
        value_fn=lambda data, loc: (
            rated.current_month.total.inc_vat
            if (rated := data.rated_cost.get(loc.id)) is not None
            else None
        ),
    ),
    OsmundLocationSensorDescription(
        key="current_month_subscriptions_cost",
        translation_key="current_month_subscriptions_cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_DKK,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        required_scope=SCOPE_COSTS_READ,
        value_fn=lambda data, loc: (
            rated.current_month.subscriptions.inc_vat
            if (rated := data.rated_cost.get(loc.id)) is not None
            else None
        ),
    ),
    OsmundLocationSensorDescription(
        key="latest_daily_cost",
        translation_key="latest_daily_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_DKK,
        suggested_display_precision=2,
        required_scope=SCOPE_COSTS_READ,
        value_fn=_latest_daily_cost_value,
        attrs_fn=_latest_daily_cost_attrs,
    ),
    OsmundLocationSensorDescription(
        key="rated_through",
        translation_key="rated_through",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        required_scope=SCOPE_COSTS_READ,
        value_fn=lambda _data, loc: parse_iso_date(loc.rated_through),
    ),
    OsmundLocationSensorDescription(
        key="supply_state",
        translation_key="supply_state",
        device_class=SensorDeviceClass.ENUM,
        options=SUPPLY_STATE_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda _data, loc: (
            loc.state if loc.state in SUPPLY_STATE_OPTIONS else None
        ),
    ),
    OsmundLocationSensorDescription(
        key="plus_status",
        translation_key="plus_status",
        device_class=SensorDeviceClass.ENUM,
        options=PLUS_STATUS_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda _data, loc: (
            loc.plus.status if loc.plus.status in PLUS_STATUS_OPTIONS else None
        ),
    ),
    OsmundLocationSensorDescription(
        key="meter_resolution",
        translation_key="meter_resolution",
        device_class=SensorDeviceClass.ENUM,
        options=METER_RESOLUTION_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda _data, loc: (
            API_TO_OPTION_RESOLUTION.get(loc.meter_resolution)
            if loc.meter_resolution is not None
            else None
        ),
    ),
)


def _latest_invoice_attrs(data: AccountData) -> dict[str, Any] | None:
    if data.invoices is None or data.invoices.latest_invoice is None:
        return None
    inv = data.invoices.latest_invoice
    return {
        "invoice_no": inv.invoice_no,
        "invoice_date": inv.invoice_date,
        "due_date": inv.due_date,
        "paid": inv.paid,
        "type": inv.type,
    }


ACCOUNT_SENSOR_DESCRIPTIONS: tuple[OsmundAccountSensorDescription, ...] = (
    OsmundAccountSensorDescription(
        key="latest_invoice_total",
        translation_key="latest_invoice_total",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_DKK,
        suggested_display_precision=2,
        required_scope=SCOPE_COSTS_READ,
        value_fn=lambda data: (
            data.invoices.latest_invoice.total.inc_vat
            if data.invoices is not None and data.invoices.latest_invoice is not None
            else None
        ),
        attrs_fn=_latest_invoice_attrs,
    ),
    OsmundAccountSensorDescription(
        key="outstanding_balance",
        translation_key="outstanding_balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_DKK,
        suggested_display_precision=2,
        required_scope=SCOPE_COSTS_READ,
        value_fn=lambda data: (
            data.invoices.outstanding_balance if data.invoices is not None else None
        ),
    ),
    OsmundAccountSensorDescription(
        key="next_invoice_due_date",
        translation_key="next_invoice_due_date",
        device_class=SensorDeviceClass.DATE,
        required_scope=SCOPE_COSTS_READ,
        value_fn=lambda data: (
            data.invoices.next_due_date if data.invoices is not None else None
        ),
    ),
    OsmundAccountSensorDescription(
        key="api_key_expires_at",
        translation_key="api_key_expires_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.key_details.expires_at_dt,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OsmundConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Osmund Energi sensor entities from a config entry."""
    runtime = entry.runtime_data
    account_coordinator = runtime.account_coordinator
    price_coordinator = runtime.price_coordinator
    key_details = runtime.key_details

    entities: list[SensorEntity] = []

    for location in account_coordinator.open_locations:
        if key_details.has_scope(SCOPE_PRICES_READ):
            entities.extend(
                OsmundPriceSensor(price_coordinator, location, description)
                for description in PRICE_SENSOR_DESCRIPTIONS
            )

        for loc_desc in LOCATION_SENSOR_DESCRIPTIONS:
            if loc_desc.required_scope is None or key_details.has_scope(
                loc_desc.required_scope
            ):
                entities.append(
                    OsmundLocationSensor(account_coordinator, location, loc_desc)
                )

    for acct_desc in ACCOUNT_SENSOR_DESCRIPTIONS:
        if acct_desc.required_scope is None or key_details.has_scope(
            acct_desc.required_scope
        ):
            entities.append(
                OsmundAccountSensor(account_coordinator, entry, acct_desc)
            )

    async_add_entities(entities)

    if key_details.has_scope(SCOPE_PRICES_READ):

        @callback
        def _handle_price_boundary(_now: datetime) -> None:
            """Advance price states on quarter-hour boundaries with 0 API calls."""
            if not price_coordinator.promote_tomorrow_if_midnight():
                price_coordinator.async_update_listeners()

        entry.async_on_unload(
            async_track_time_change(
                hass,
                _handle_price_boundary,
                minute=(0, 15, 30, 45),
                second=1,
            )
        )


class OsmundPriceSensor(CoordinatorEntity[OsmundPriceCoordinator], SensorEntity):
    """Electricity price sensor backed by OsmundPriceCoordinator."""

    entity_description: OsmundPriceSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OsmundPriceCoordinator,
        location: Location,
        description: OsmundPriceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._location_id = location.id
        self._attr_unique_id = f"{location.id}_{description.key}"
        self._attr_device_info = _location_device_info(location)

    @property
    def _location_prices(self) -> LocationPrices | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._location_id)

    @property
    def available(self) -> bool:
        """Return True when price data for this location is loaded."""
        return super().available and self._location_prices is not None

    @property
    def native_value(self) -> float | datetime | None:
        """Return the current sensor value."""
        prices = self._location_prices
        if prices is None:
            return None
        return self.entity_description.value_fn(prices, danish_now())


class OsmundLocationSensor(CoordinatorEntity[OsmundAccountCoordinator], SensorEntity):
    """Per-location metered/diagnostic sensor backed by OsmundAccountCoordinator."""

    entity_description: OsmundLocationSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OsmundAccountCoordinator,
        location: Location,
        description: OsmundLocationSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._location_id = location.id
        self._attr_unique_id = f"{location.id}_{description.key}"
        self._attr_device_info = _location_device_info(location)

    @property
    def _location(self) -> Location | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.locations.get(self._location_id)

    @property
    def available(self) -> bool:
        """Return True when the location is present and has open API access."""
        loc = self._location
        return super().available and loc is not None and loc.has_api_access

    @property
    def native_value(self) -> float | str | date | datetime | None:
        """Return the current sensor value."""
        if self.coordinator.data is None or (loc := self._location) is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, loc)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return optional compact metadata attributes."""
        if (
            self.entity_description.attrs_fn is None
            or self.coordinator.data is None
            or (loc := self._location) is None
        ):
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, loc)


class OsmundAccountSensor(CoordinatorEntity[OsmundAccountCoordinator], SensorEntity):
    """Account-level sensor backed by OsmundAccountCoordinator."""

    entity_description: OsmundAccountSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OsmundAccountCoordinator,
        entry: OsmundConfigEntry,
        description: OsmundAccountSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _account_device_info(entry)

    @property
    def native_value(self) -> float | date | datetime | None:
        """Return the current sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return optional compact metadata attributes."""
        if self.entity_description.attrs_fn is None or self.coordinator.data is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
