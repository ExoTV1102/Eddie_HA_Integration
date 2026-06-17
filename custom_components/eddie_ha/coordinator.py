"""Runtime coordinator for EDDIE Home Assistant push data."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, UTC
import json
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import EddieHaClient
from .const import DOMAIN, SIGNAL_READING_DISCOVERED

_LOGGER = logging.getLogger(__name__)

_OBIS_RE = re.compile(r"^\d+-\d+:\d+\.\d+\.\d+(?:\*\d+)?$")
_AIIDA_QUANTITY_TYPE_TO_OBIS = {
    "0": "1-0:1.8.0",
    "2": "1-0:2.8.0",
    "TOTALACTIVEENERGYCONSUMED_IMPORT_KWH": "1-0:1.8.0",
    "TOTALACTIVEENERGYPRODUCED_EXPORT_KWH": "1-0:2.8.0",
    "INSTANTANEOUSACTIVEPOWERCONSUMPTION_IMPORT__KW": "1-0:1.7.0",
    "INSTANTANEOUSACTIVEPOWERGENERATION_EXPORT_KW": "1-0:2.7.0",
}
_TASMOTA_TO_OBIS = {
    "E_in": ("1-0:1.8.0", "kWh"),
    "E_out": ("1-0:2.8.0", "kWh"),
    "Power": ("1-0:16.7.0", "W"),
}
_ENERGY_OBIS = {"1-0:1.8.0", "1-0:2.8.0"}
_POWER_OBIS = {"1-0:1.7.0", "1-0:2.7.0", "1-0:16.7.0"}


@dataclass(frozen=True)
class EddieReading:
    """A single reading exposed to Home Assistant as a sensor."""

    key: str
    name: str
    value: float | str
    unit: str | None
    device_class: str | None
    state_class: str | None
    last_updated: datetime


class EddieHaCoordinator:
    """Coordinate the EDDIE WebSocket and discovered readings."""

    def __init__(self, hass: HomeAssistant, client: EddieHaClient, entry_id: str) -> None:
        self.hass = hass
        self.client = client
        self.entry_id = entry_id
        self.readings: dict[str, EddieReading] = {}
        self.last_message: dict[str, Any] | None = None
        self.last_message_at: datetime | None = None
        self.connected = False
        self._listeners: list[Callable[[], None]] = []
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a listener for coordinator updates."""
        self._listeners.append(listener)

        @callback
        def remove_listener() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove_listener

    @callback
    def async_update_listeners(self) -> None:
        """Notify listeners."""
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        """Start the EDDIE listener."""
        if self._task is None:
            self._task = self.hass.async_create_task(self._run())

    async def async_stop(self) -> None:
        """Stop the EDDIE listener."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        await self.client.listen(self._handle_message, self._stop_event)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        self.last_message = message
        self.last_message_at = datetime.now(UTC)
        self.connected = True

        discovered = False
        for reading in extract_readings(message, self.last_message_at):
            is_new = reading.key not in self.readings
            self.readings[reading.key] = reading
            if is_new:
                discovered = True

        self.async_update_listeners()
        if discovered:
            async_dispatcher_send(self.hass, f"{SIGNAL_READING_DISCOVERED}_{self.entry_id}")


def extract_readings(message: dict[str, Any], timestamp: datetime) -> list[EddieReading]:
    """Extract useful sensor readings from a generic EDDIE payload."""
    payload = _decode_payload(message.get("payload", message))
    readings: dict[str, EddieReading] = {}
    _walk_payload(payload, [], readings, timestamp)
    return list(readings.values())


def _walk_payload(
    value: Any,
    path: list[str],
    readings: dict[str, EddieReading],
    timestamp: datetime,
) -> None:
    value = _decode_payload(value)

    if isinstance(value, dict):
        obis = _first_string(value, "obis", "obisCode", "dataTag", "data_tag", "tag")
        quantity = _first_number(value, "value", "quantity", "energy_Quantity.quantity", "amount")
        if obis and quantity is not None:
            unit = _first_string(
                value,
                "unit",
                "unitOfMeasure",
                "unit_of_measure",
                "measurementUnit",
                "unitOfMeasurement",
            )
            readings[obis] = _make_reading(obis, quantity, unit, timestamp)

        _extract_tasmota_values(value, readings, timestamp)
        _extract_cim_values(value, readings, timestamp)

        for key, child in value.items():
            _walk_payload(child, [*path, str(key)], readings, timestamp)
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk_payload(child, [*path, str(index)], readings, timestamp)
        return

    if isinstance(value, (int, float)) and path:
        key = path[-1]
        if _OBIS_RE.match(key):
            readings[key] = _make_reading(key, value, None, timestamp)


def _decode_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _extract_tasmota_values(
    data: dict[str, Any],
    readings: dict[str, EddieReading],
    timestamp: datetime,
) -> None:
    for raw_key, (obis, unit) in _TASMOTA_TO_OBIS.items():
        value = data.get(raw_key)
        if isinstance(value, (int, float)):
            readings[obis] = _make_reading(obis, float(value), unit, timestamp)


def _extract_cim_values(
    data: dict[str, Any],
    readings: dict[str, EddieReading],
    timestamp: datetime,
) -> None:
    quantities = data.get("Quantity", data.get("quantities"))
    if not isinstance(quantities, list):
        return

    for item in quantities:
        if not isinstance(item, dict):
            continue

        type_value = item.get("type")
        if type_value is None:
            continue

        obis = _AIIDA_QUANTITY_TYPE_TO_OBIS.get(str(type_value))
        quantity = _first_number(item, "quantity", "value", "amount")
        if obis and quantity is not None:
            readings[obis] = _make_reading(obis, quantity, None, timestamp)


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _make_reading(key: str, value: float | str, unit: str | None, timestamp: datetime) -> EddieReading:
    device_class = None
    state_class = None
    normalized_unit = unit
    normalized_value = value

    if key in _ENERGY_OBIS:
        device_class = "energy"
        state_class = "total_increasing"
        normalized_value = _normalize_energy_value(value, unit)
        normalized_unit = "kWh"
    elif key in _POWER_OBIS:
        device_class = "power"
        state_class = "measurement"
        normalized_value = _normalize_power_value(value, unit)
        normalized_unit = "W"

    return EddieReading(
        key=key,
        name=f"EDDIE {key}",
        value=normalized_value,
        unit=normalized_unit,
        device_class=device_class,
        state_class=state_class,
        last_updated=timestamp,
    )


def _normalize_energy_value(value: float | str, unit: str | None) -> float | str:
    if not isinstance(value, (int, float)):
        return value
    if unit in {"Wh", "WATT_HOUR"}:
        return _clean_zero(value / 1000)
    return _clean_zero(float(value))


def _normalize_power_value(value: float | str, unit: str | None) -> float | str:
    if not isinstance(value, (int, float)):
        return value
    if unit in {"kW", "KW", "KILO_WATT"}:
        return _clean_zero(value * 1000)
    return _clean_zero(float(value))


def _clean_zero(value: float) -> float:
    if value == 0:
        return 0.0
    return value
