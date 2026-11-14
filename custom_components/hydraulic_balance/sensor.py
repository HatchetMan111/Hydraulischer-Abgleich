"""Sensor platform: global status plus per-room rate/opening/status sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_FLOW_SENSOR, CONF_RETURN_SENSOR, DOMAIN, MANUFACTURER
from .coordinator import HydraulicBalanceCoordinator, RoomRuntime
from .learning import DEFAULT_MAX_OPENING, evaluate_delta_t


class _BalanceEntity(SensorEntity):
    _attr_should_poll = False

    def __init__(self, coordinator: HydraulicBalanceCoordinator, entry: ConfigEntry) -> None:
        self._coord = coordinator
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Hydraulik-Abgleich",
            manufacturer=MANUFACTURER,
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._coord.async_add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coord.async_remove_listener(self._handle_update)


class GlobalStatusSensor(_BalanceEntity):
    _attr_name = "Hydraulik-Abgleich Status"
    _attr_icon = "mdi:pipe-valve"

    def __init__(self, coordinator: HydraulicBalanceCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-status"

    @property
    def native_value(self) -> str:
        return self._coord.phase

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self._coord
        return {
            "zyklus": coord.cycles_done,
            "konvergiert": coord.converged,
            "median_kph": None if coord.last_median is None else round(coord.last_median, 3),
            "hinweise": list(coord.advice),
            "bericht": list(coord.report[-50:]),
            "oeffnungsgrade": {k: int(round(v)) for k, v in coord.learning.items()},
        }


class RoomRateSensor(_BalanceEntity):
    _attr_native_unit_of_measurement = "K/h"
    _attr_icon = "mdi:trending-up"

    def __init__(self, coordinator: HydraulicBalanceCoordinator, entry: ConfigEntry, room: RoomRuntime) -> None:
        super().__init__(coordinator, entry)
        self._room = room
        self._attr_unique_id = f"{entry.entry_id}-rate-{room.name}"
        self._attr_name = f"{room.name} Heizrate"

    @property
    def native_value(self) -> float | None:
        return None if self._room.rate is None else round(self._room.rate, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self._room
        return {
            "starttemperatur": room.start_temp,
            "zieltemperatur": room.target,
            "hat_ziel_erreicht": room.reached,
            "messpunkte": len(room.samples),
        }


class RoomOpeningSensor(_BalanceEntity):
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:valve"

    def __init__(self, coordinator: HydraulicBalanceCoordinator, entry: ConfigEntry, room: RoomRuntime) -> None:
        super().__init__(coordinator, entry)
        self._room = room
        self._attr_unique_id = f"{entry.entry_id}-opening-{room.name}"
        self._attr_name = f"{room.name} Öffnungsgrad-Ziel"

    @property
    def native_value(self) -> int:
        return int(round(self._coord.learning.get(self._room.name, DEFAULT_MAX_OPENING)))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"begründung": self._room.reason}


class RoomStatusSensor(_BalanceEntity):
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator: HydraulicBalanceCoordinator, entry: ConfigEntry, room: RoomRuntime) -> None:
        super().__init__(coordinator, entry)
        self._room = room
        self._attr_unique_id = f"{entry.entry_id}-status-{room.name}"
        self._attr_name = f"{room.name} Abgleich-Status"

    @property
    def native_value(self) -> str:
        return self._room.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"begründung": self._room.reason, "heizrate_kph": self._room.rate}


class _PipeSensorEntity(_BalanceEntity):
    """Basis für Sensoren, die von Vor-/Rücklauf-Temperatursensoren leben."""

    def __init__(self, coordinator: HydraulicBalanceCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._flow_id = str(entry.options.get(CONF_FLOW_SENSOR, "") or "")
        self._return_id = str(entry.options.get(CONF_RETURN_SENSOR, "") or "")
        self._unsub_track = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_track = async_track_state_change_event(
            self.hass, [self._flow_id, self._return_id], self._on_state_change
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub_track:
            self._unsub_track()
            self._unsub_track = None

    @callback
    def _on_state_change(self, event) -> None:
        self.async_write_ha_state()

    def _read_temps(self) -> tuple[float | None, float | None]:
        flow = HydraulicBalanceCoordinator._state_float(self.hass, self._flow_id)
        ret = HydraulicBalanceCoordinator._state_float(self.hass, self._return_id)
        return flow, ret


class DeltaTSensor(_PipeSensorEntity):
    """Spreizung (ΔT) zwischen Vorlauf und Rücklauf in Kelvin."""

    _attr_name = "Hydraulik-Abgleich Spreizung (ΔT)"
    _attr_native_unit_of_measurement = "K"
    _attr_icon = "mdi:heat-pump"

    def __init__(self, coordinator: HydraulicBalanceCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-delta-t"

    @property
    def native_value(self) -> float | None:
        result = evaluate_delta_t(*self._read_temps())
        return None if result is None else result["delta"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        flow, ret = self._read_temps()
        result = evaluate_delta_t(flow, ret)
        return {
            "vorlauf": flow,
            "ruecklauf": ret,
            "status": None if result is None else result["status"],
            "hinweis": None if result is None else result["advice"],
        }


class EfficiencyStatusSensor(_PipeSensorEntity):
    """Effizienz-Bewertung aus der Spreizung (Rücklauf-Minimierung)."""

    _attr_name = "Hydraulik-Abgleich Effizienz"
    _attr_icon = "mdi:leaf"

    def __init__(self, coordinator: HydraulicBalanceCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-effizienz"

    @property
    def native_value(self) -> str:
        result = evaluate_delta_t(*self._read_temps())
        return "unbekannt" if result is None else result["status"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        flow, ret = self._read_temps()
        result = evaluate_delta_t(flow, ret)
        return {
            "delta_t": None if result is None else result["delta"],
            "vorlauf": flow,
            "ruecklauf": ret,
            "hinweis": None if result is None else result["advice"],
        }


async def async_setup_entry(hass, entry, async_add_entities):
    coord: HydraulicBalanceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [GlobalStatusSensor(coord, entry)]
    for room in coord.rooms:
        entities.append(RoomRateSensor(coord, entry, room))
        entities.append(RoomOpeningSensor(coord, entry, room))
        entities.append(RoomStatusSensor(coord, entry, room))
    if entry.options.get(CONF_FLOW_SENSOR) and entry.options.get(CONF_RETURN_SENSOR):
        entities.append(DeltaTSensor(coord, entry))
        entities.append(EfficiencyStatusSensor(coord, entry))
    async_add_entities(entities)
