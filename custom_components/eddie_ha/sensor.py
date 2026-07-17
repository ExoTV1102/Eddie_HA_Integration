"""Sensors for EDDIE Home Assistant."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_READING_DISCOVERED
from .coordinator import EddieHaCoordinator, EddieReading


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EDDIE sensors."""
    coordinator: EddieHaCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_keys: set[str] = set()

    @callback
    def add_discovered_entities() -> None:
        entities = []
        for key in coordinator.readings:
            if key not in known_keys:
                known_keys.add(key)
                entities.append(EddieReadingSensor(coordinator, entry.entry_id, key))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{SIGNAL_READING_DISCOVERED}_{entry.entry_id}",
            add_discovered_entities,
        )
    )

    async_add_entities([EddieStatusSensor(coordinator, entry.entry_id)])
    add_discovered_entities()


class EddieBaseSensor(SensorEntity):
    """Base EDDIE sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EddieHaCoordinator) -> None:
        self.coordinator = coordinator
        self._remove_listener = coordinator.async_add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Clean up listener."""
        self._remove_listener()


class EddieStatusSensor(EddieBaseSensor):
    """Diagnostic connection status sensor."""

    _attr_name = "Connection"
    _attr_icon = "mdi:connection"

    def __init__(self, coordinator: EddieHaCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_connection"

    @property
    def native_value(self) -> str:
        """Return the current connection status."""
        return self.coordinator.connection_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes."""
        last_message_at = self.coordinator.last_message_at
        message = self.coordinator.last_message or {}
        payload = message.get("payload")

        return {
            "last_message_at": last_message_at.isoformat() if last_message_at else None,
            "last_message_type": message.get("type"),
            "last_data_need_id": message.get("dataNeedId"),
            "last_connection_id": message.get("connectionId"),
            "readings": len(self.coordinator.readings),
            "reading_keys": sorted(self.coordinator.readings),
            "last_payload_preview": _payload_preview(payload),
        }


class EddieReadingSensor(EddieBaseSensor):
    """Sensor for one EDDIE reading."""

    def __init__(self, coordinator: EddieHaCoordinator, entry_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"{entry_id}_{key.replace(':', '_').replace('.', '_').replace('*', '_')}"

    @property
    def device_info(self) -> dict[str, Any] | None:
        """Group Hager readings by their Modbus source in the HA device registry."""
        reading = self.reading
        if reading is None or reading.device_id is None:
            return None
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.entry_id}_{reading.device_id}")},
            "name": reading.device_name,
            "manufacturer": "Hager",
            "model": "Flow",
        }

    @property
    def reading(self) -> EddieReading | None:
        """Return the current reading."""
        return self.coordinator.readings.get(self.key)

    @property
    def name(self) -> str | None:
        """Return the sensor name."""
        return self.reading.name if self.reading else self.key

    @property
    def native_value(self) -> float | str | None:
        """Return the current value."""
        return self.reading.value if self.reading else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the native unit."""
        unit = self.reading.unit if self.reading else None
        if unit == "kWh":
            return UnitOfEnergy.KILO_WATT_HOUR
        if unit == "Wh":
            return UnitOfEnergy.WATT_HOUR
        if unit == "W":
            return UnitOfPower.WATT
        if unit == "A":
            return UnitOfElectricCurrent.AMPERE
        if unit == "V":
            return UnitOfElectricPotential.VOLT
        if unit == "VA":
            return UnitOfApparentPower.VOLT_AMPERE
        if unit == "var":
            return "var"
        if unit == "Hz":
            return UnitOfFrequency.HERTZ
        return unit

    @property
    def device_class(self) -> str | None:
        """Return the device class."""
        if self.reading is None:
            return None
        if self.reading.device_class == "energy":
            return SensorDeviceClass.ENERGY
        if self.reading.device_class == "power":
            return SensorDeviceClass.POWER
        if self.reading.device_class == "current":
            return SensorDeviceClass.CURRENT
        if self.reading.device_class == "voltage":
            return SensorDeviceClass.VOLTAGE
        if self.reading.device_class == "battery":
            return SensorDeviceClass.BATTERY
        if self.reading.device_class == "reactive_power":
            return SensorDeviceClass.REACTIVE_POWER
        if self.reading.device_class == "apparent_power":
            return SensorDeviceClass.APPARENT_POWER
        if self.reading.device_class == "power_factor":
            return SensorDeviceClass.POWER_FACTOR
        if self.reading.device_class == "frequency":
            return SensorDeviceClass.FREQUENCY
        return None

    @property
    def state_class(self) -> str | None:
        """Return the state class."""
        if self.reading is None:
            return None
        if self.reading.state_class == "measurement":
            return SensorStateClass.MEASUREMENT
        if self.reading.state_class == "total_increasing":
            return SensorStateClass.TOTAL_INCREASING
        if self.reading.state_class == "total":
            return SensorStateClass.TOTAL
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return reading attributes."""
        reading = self.reading
        return {
            "raw_tag": reading.raw_tag if reading else None,
            "data_tag": reading.data_tag if reading else None,
            "source_value": reading.source_value if reading else None,
            "last_updated": reading.last_updated.isoformat() if reading else None,
        }


def _payload_preview(payload: Any) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload[:1000]
    return json.dumps(payload, ensure_ascii=False, default=str)[:1000]
