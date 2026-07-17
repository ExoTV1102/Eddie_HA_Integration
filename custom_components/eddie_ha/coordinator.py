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

_OBIS_RE = re.compile(r"^\d+-\d+:[0-9A-Za-z]+\.\d+\.\d+(?:\*\d+)?$")
_ACTIVE_ENERGY_TARIFF_RE = re.compile(
    r"^1-0:(1|2|21|22|41|42|61|62)\.8\.(\d+)$"
)
_REACTIVE_ENERGY_TARIFF_RE = re.compile(r"^1-0:(3|4)\.8\.(\d+)$")
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
_UNKNOWN_DATA_TAG = "0-0:0.0.0"
_HAGER_SOURCE_NAMES = {
    "hager-flow-system": "Hager Flow",
    "hager-flow-ems": "Hager Flow EMS",
    "hager-flow-evse-1": "Hager Flow Wallbox 1",
    "hager-flow-storage": "Hager Flow Storage",
    "hager-flow-power-meter-1": "Hager Flow Power Meter 1",
}
_HAGER_TOTAL_INCREASING_ENERGY = {
    "evcs_energy_all",
    "evcs_solar_energy",
}


@dataclass(frozen=True)
class _ObisDescription:
    name: str
    unit: str | None
    device_class: str | None
    state_class: str | None


def _obis_measurement(name: str, unit: str, device_class: str) -> _ObisDescription:
    return _ObisDescription(name, unit, device_class, "measurement")


def _obis_total(name: str, unit: str, device_class: str | None) -> _ObisDescription:
    return _ObisDescription(name, unit, device_class, "total_increasing")


_OBIS_DESCRIPTIONS = {
    "1-0:1.8.0": _obis_total("Imported Active Energy", "kWh", "energy"),
    "1-0:21.8.0": _obis_total("Imported Active Energy L1", "kWh", "energy"),
    "1-0:41.8.0": _obis_total("Imported Active Energy L2", "kWh", "energy"),
    "1-0:61.8.0": _obis_total("Imported Active Energy L3", "kWh", "energy"),
    "1-0:2.8.0": _obis_total("Exported Active Energy", "kWh", "energy"),
    "1-0:22.8.0": _obis_total("Exported Active Energy L1", "kWh", "energy"),
    "1-0:42.8.0": _obis_total("Exported Active Energy L2", "kWh", "energy"),
    "1-0:62.8.0": _obis_total("Exported Active Energy L3", "kWh", "energy"),
    "1-0:1.7.0": _obis_measurement("Imported Active Power", "kW", "power"),
    "1-0:21.7.0": _obis_measurement("Imported Active Power L1", "kW", "power"),
    "1-0:41.7.0": _obis_measurement("Imported Active Power L2", "kW", "power"),
    "1-0:61.7.0": _obis_measurement("Imported Active Power L3", "kW", "power"),
    "1-0:2.7.0": _obis_measurement("Exported Active Power", "kW", "power"),
    "1-0:22.7.0": _obis_measurement("Exported Active Power L1", "kW", "power"),
    "1-0:42.7.0": _obis_measurement("Exported Active Power L2", "kW", "power"),
    "1-0:62.7.0": _obis_measurement("Exported Active Power L3", "kW", "power"),
    "1-0:16.7.0": _obis_measurement("Total Active Power", "W", "power"),
    "1-0:3.7.0": _obis_measurement("Imported Reactive Power", "kVAr", "reactive_power"),
    "1-0:23.7.0": _obis_measurement("Imported Reactive Power L1", "kVAr", "reactive_power"),
    "1-0:43.7.0": _obis_measurement("Imported Reactive Power L2", "kVAr", "reactive_power"),
    "1-0:63.7.0": _obis_measurement("Imported Reactive Power L3", "kVAr", "reactive_power"),
    "1-0:4.7.0": _obis_measurement("Exported Reactive Power", "kVAr", "reactive_power"),
    "1-0:24.7.0": _obis_measurement("Exported Reactive Power L1", "kVAr", "reactive_power"),
    "1-0:44.7.0": _obis_measurement("Exported Reactive Power L2", "kVAr", "reactive_power"),
    "1-0:64.7.0": _obis_measurement("Exported Reactive Power L3", "kVAr", "reactive_power"),
    "1-0:3.8.0": _obis_total("Imported Reactive Energy", "kVArh", None),
    "1-0:4.8.0": _obis_total("Exported Reactive Energy", "kVArh", None),
    "1-0:3.8.1": _obis_total("Imported Reactive Energy Tariff 1", "kVArh", None),
    "1-0:4.8.1": _obis_total("Exported Reactive Energy Tariff 1", "kVArh", None),
    "1-0:11.6.0": _obis_measurement("Maximum Current", "A", "current"),
    "1-0:31.6.0": _obis_measurement("Maximum Current L1", "A", "current"),
    "1-0:51.6.0": _obis_measurement("Maximum Current L2", "A", "current"),
    "1-0:71.6.0": _obis_measurement("Maximum Current L3", "A", "current"),
    "1-0:9.7.0": _obis_measurement("Apparent Power", "kVA", "apparent_power"),
    "1-0:29.7.0": _obis_measurement("Apparent Power L1", "kVA", "apparent_power"),
    "1-0:49.7.0": _obis_measurement("Apparent Power L2", "kVA", "apparent_power"),
    "1-0:69.7.0": _obis_measurement("Apparent Power L3", "kVA", "apparent_power"),
    "1-0:11.7.0": _obis_measurement("Current", "A", "current"),
    "1-0:31.7.0": _obis_measurement("Current L1", "A", "current"),
    "1-0:51.7.0": _obis_measurement("Current L2", "A", "current"),
    "1-0:71.7.0": _obis_measurement("Current L3", "A", "current"),
    "1-0:91.7.0": _obis_measurement("Neutral Current", "A", "current"),
    "1-0:13.7.0": _obis_measurement("Power Factor", "none", "power_factor"),
    "1-0:33.7.0": _obis_measurement("Power Factor L1", "none", "power_factor"),
    "1-0:53.7.0": _obis_measurement("Power Factor L2", "none", "power_factor"),
    "1-0:73.7.0": _obis_measurement("Power Factor L3", "none", "power_factor"),
    "1-0:12.7.0": _obis_measurement("Voltage", "V", "voltage"),
    "1-0:32.7.0": _obis_measurement("Voltage L1", "V", "voltage"),
    "1-0:52.7.0": _obis_measurement("Voltage L2", "V", "voltage"),
    "1-0:72.7.0": _obis_measurement("Voltage L3", "V", "voltage"),
    "1-0:14.7.0": _obis_measurement("Grid Frequency", "Hz", "frequency"),
    "0-0:96.1.0": _ObisDescription("Device ID", None, None, None),
    "0-0:1.0.0": _ObisDescription("Meter Time", None, None, None),
    "0-0:2.0.0": _ObisDescription("Meter Uptime", None, None, None),
    "0-0:C.1.0": _ObisDescription("Meter Serial", None, None, None),
}


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
    raw_tag: str | None = None
    data_tag: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    source_value: Any = None


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
        self.connection_status = "waiting_for_data"
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
        await self.client.listen(
            self._handle_message,
            self._stop_event,
            self._handle_connection_state,
        )

    async def _handle_connection_state(self, connected: bool) -> None:
        self.connected = connected
        self.connection_status = "connected" if connected else "disconnected"
        self.async_update_listeners()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        self.last_message = message
        self.last_message_at = datetime.now(UTC)
        self.connected = True
        self.connection_status = "connected"

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
        raw_tag = _first_string(value, "rawTag", "raw_tag")
        if raw_tag and raw_tag.startswith("hager-flow-"):
            hager_value = _first_scalar(value, "value", "rawValue", "raw_value")
            if hager_value is not None:
                readings[raw_tag] = _make_hager_reading(
                    raw_tag,
                    obis,
                    hager_value,
                    _measurement_unit(value),
                    timestamp,
                )

        obis_value = _first_scalar(
            value,
            "value",
            "quantity",
            "energy_Quantity.quantity",
            "amount",
            "rawValue",
            "raw_value",
        )
        if obis and obis != _UNKNOWN_DATA_TAG and obis_value is not None:
            readings[obis] = _make_reading(
                obis,
                _normalize_hager_value(obis_value),
                _measurement_unit(value),
                timestamp,
            )

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


def _first_scalar(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _measurement_unit(data: dict[str, Any]) -> str | None:
    unit = _first_string(
        data,
        "unit",
        "unitOfMeasure",
        "unit_of_measure",
        "measurementUnit",
        "unitOfMeasurement",
    )
    if unit and unit.lower() not in {"unknown", "none"}:
        return unit

    raw_unit = _first_string(data, "rawUnitOfMeasurement", "raw_unit")
    return raw_unit if raw_unit and raw_unit.lower() != "unknown" else None


def _make_hager_reading(
    raw_tag: str,
    data_tag: str | None,
    source_value: Any,
    unit: str | None,
    timestamp: datetime,
) -> EddieReading:
    source_id, _, datapoint_id = raw_tag.partition("::")
    value = _normalize_hager_value(source_value)
    if (
        source_id == "hager-flow-ems"
        and datapoint_id.startswith("root_power_")
        and isinstance(value, float)
        and value > 0x7FFFFFFF
    ):
        value -= 0x100000000
    device_class = None
    state_class = None

    if unit in {"Wh", "kWh"}:
        device_class = "energy"
        state_class = (
            "total_increasing"
            if datapoint_id in _HAGER_TOTAL_INCREASING_ENERGY
            else "total"
        )
    elif unit in {"W", "kW", "KW"}:
        device_class = "power"
        state_class = "measurement"
    elif unit == "A":
        device_class = "current"
        state_class = "measurement"
    elif unit == "V":
        device_class = "voltage"
        state_class = "measurement"
    elif unit == "%":
        if datapoint_id == "battery_state_of_charge":
            device_class = "battery"
        state_class = "measurement"

    return EddieReading(
        key=raw_tag,
        name=datapoint_id.replace("_", " ").title(),
        value=value,
        unit=unit,
        device_class=device_class,
        state_class=state_class,
        last_updated=timestamp,
        raw_tag=raw_tag,
        data_tag=data_tag if data_tag != _UNKNOWN_DATA_TAG else None,
        device_id=source_id,
        device_name=_HAGER_SOURCE_NAMES.get(source_id, source_id.replace("-", " ").title()),
        source_value=source_value,
    )


def _normalize_hager_value(value: Any) -> float | str:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return str(value)

    text = value.strip("\x00\r\n ")
    try:
        return float(text.replace(",", "."))
    except ValueError:
        pass

    if text.startswith("["):
        try:
            array = json.loads(text)
        except json.JSONDecodeError:
            return text[:255]
        if isinstance(array, list) and len(text) > 255:
            return f"{len(array)} values"
    return text[:255]


def _make_reading(key: str, value: float | str, unit: str | None, timestamp: datetime) -> EddieReading:
    description = _obis_description(key)
    if description.device_class is None and description.unit is None:
        description = _description_from_unit(description.name, unit)
    normalized_unit = unit or description.unit
    normalized_value = value

    if description.device_class == "reactive_power":
        if normalized_unit in {"kVAr", "kvar"} and isinstance(value, float):
            normalized_value = value * 1000
        normalized_unit = "var"
    elif description.device_class == "apparent_power":
        if normalized_unit == "kVA" and isinstance(value, float):
            normalized_value = value * 1000
        normalized_unit = "VA"
    elif normalized_unit == "none":
        normalized_unit = None

    return EddieReading(
        key=key,
        name=description.name,
        value=normalized_value,
        unit=normalized_unit,
        device_class=description.device_class,
        state_class=description.state_class,
        last_updated=timestamp,
        data_tag=key,
        source_value=value,
    )


def _obis_description(key: str) -> _ObisDescription:
    normalized_key = key.split("*", maxsplit=1)[0]
    description = _OBIS_DESCRIPTIONS.get(normalized_key)
    if description is not None:
        return description

    tariff_match = _ACTIVE_ENERGY_TARIFF_RE.match(normalized_key)
    if tariff_match is not None:
        register, tariff = tariff_match.groups()
        direction = "Imported" if register in {"1", "21", "41", "61"} else "Exported"
        phase = {
            "21": " L1",
            "22": " L1",
            "41": " L2",
            "42": " L2",
            "61": " L3",
            "62": " L3",
        }.get(register, "")
        tariff_name = " Total" if tariff == "0" else f" Tariff {tariff}"
        return _obis_total(f"{direction} Active Energy{phase}{tariff_name}", "kWh", "energy")

    reactive_match = _REACTIVE_ENERGY_TARIFF_RE.match(normalized_key)
    if reactive_match is not None:
        register, tariff = reactive_match.groups()
        direction = "Imported" if register == "3" else "Exported"
        tariff_name = " Total" if tariff == "0" else f" Tariff {tariff}"
        return _obis_total(f"{direction} Reactive Energy{tariff_name}", "kVArh", None)

    return _ObisDescription(f"OBIS {key}", None, None, None)


def _description_from_unit(name: str, unit: str | None) -> _ObisDescription:
    if unit in {"Wh", "kWh"}:
        return _obis_total(name, unit, "energy")
    if unit in {"W", "kW", "KW"}:
        return _obis_measurement(name, unit, "power")
    if unit in {"VAr", "kVAr", "var", "kvar"}:
        return _obis_measurement(name, unit, "reactive_power")
    if unit in {"VA", "kVA"}:
        return _obis_measurement(name, unit, "apparent_power")
    if unit == "A":
        return _obis_measurement(name, unit, "current")
    if unit == "V":
        return _obis_measurement(name, unit, "voltage")
    if unit == "Hz":
        return _obis_measurement(name, unit, "frequency")
    return _ObisDescription(name, unit, None, None)
