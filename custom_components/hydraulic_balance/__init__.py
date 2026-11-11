"""Hydraulic Balance: digitaler hydraulischer Abgleich für Zigbee-TRVs."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN
from .coordinator import HydraulicBalanceCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_START = "start"
SERVICE_STOP = "stop"
SERVICE_RESET = "reset_learning"

_ENTRY_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    coordinator = HydraulicBalanceCoordinator(hass, entry)
    await coordinator.async_load()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: HydraulicBalanceCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_START):
        return

    def _get_coordinator(call: ServiceCall) -> HydraulicBalanceCoordinator:
        coordinators = list(hass.data.get(DOMAIN, {}).values())
        entry_id = call.data.get("config_entry_id")
        if entry_id:
            for coord in coordinators:
                if coord.entry_id == entry_id:
                    return coord
            raise ValueError(f"Keine Konfiguration mit ID {entry_id}")
        if len(coordinators) == 1:
            return coordinators[0]
        raise ValueError("Mehrere Konfigurationen: bitte config_entry_id angeben")

    async def handle_start(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_start()

    async def handle_stop(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_stop()

    async def handle_reset(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_reset_learning()

    hass.services.async_register(DOMAIN, SERVICE_START, handle_start, schema=_ENTRY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP, handle_stop, schema=_ENTRY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RESET, handle_reset, schema=_ENTRY_SCHEMA)
