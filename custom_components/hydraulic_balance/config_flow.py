"""Config flow: pick rooms (thermostat + sensor + opening number) and options."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_AUTOSET,
    CONF_BOOST_CAP,
    CONF_BOOST_DELTA,
    CONF_CLIMATE,
    CONF_INTERVAL,
    CONF_MAX_CYCLES,
    CONF_MAX_OPENING_NUMBER,
    CONF_MIN_OPENING,
    CONF_NAME,
    CONF_OPENING_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_TIMEOUT,
    CONF_TOLERANCE,
    DOMAIN,
    OPTION_DEFAULTS,
)


def _number(minimum: float, maximum: float, step: float, unit: str, mode=NumberSelectorMode.BOX):
    return NumberSelector(
        NumberSelectorConfig(min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=mode)
    )


def _options_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_INTERVAL, default=defaults[CONF_INTERVAL]): _number(30, 300, 30, "s"),
            vol.Required(CONF_BOOST_DELTA, default=defaults[CONF_BOOST_DELTA]): _number(1, 6, 0.5, "K"),
            vol.Required(CONF_BOOST_CAP, default=defaults[CONF_BOOST_CAP]): _number(18, 28, 1, "°C"),
            vol.Required(CONF_MIN_OPENING, default=defaults[CONF_MIN_OPENING]): _number(0, 60, 5, "%"),
            vol.Required(CONF_TIMEOUT, default=defaults[CONF_TIMEOUT]): _number(20, 240, 10, "min"),
            vol.Required(CONF_TOLERANCE, default=defaults[CONF_TOLERANCE]): _number(5, 40, 5, "%"),
            vol.Required(CONF_MAX_CYCLES, default=defaults[CONF_MAX_CYCLES]): _number(1, 8, 1, ""),
            vol.Required(CONF_AUTOSET, default=defaults[CONF_AUTOSET]): BooleanSelector(),
        }
    )


def _room_schema(last: dict | None = None) -> vol.Schema:
    last = last or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=last.get(CONF_NAME, "")): TextSelector(),
            vol.Required(CONF_CLIMATE, default=last.get(CONF_CLIMATE)): EntitySelector(
                EntitySelectorConfig(domain="climate")
            ),
            vol.Required(CONF_TEMP_SENSOR, default=last.get(CONF_TEMP_SENSOR)): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Optional(CONF_MAX_OPENING_NUMBER, default=last.get(CONF_MAX_OPENING_NUMBER, "")): EntitySelector(
                EntitySelectorConfig(domain="number")
            ),
            vol.Optional(CONF_OPENING_SENSOR, default=last.get(CONF_OPENING_SENSOR, "")): EntitySelector(
                EntitySelectorConfig(domain="sensor")
            ),
            vol.Required("add_another", default=True): BooleanSelector(),
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow: options first, then rooms one by one."""

    VERSION = 1

    def __init__(self) -> None:
        self._opts: dict = dict(OPTION_DEFAULTS)
        self._rooms: list[dict] = []
        self._last_room: dict | None = None

    async def async_step_user(self, user_input=None):
        return await self.async_step_options()

    async def async_step_options(self, user_input=None):
        if user_input is not None:
            self._opts = user_input
            return await self.async_step_room()
        return self.async_show_form(step_id="options", data_schema=_options_schema(self._opts))

    async def async_step_room(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_NAME, "")).strip()
            climate = user_input.get(CONF_CLIMATE)
            temp_sensor = user_input.get(CONF_TEMP_SENSOR)
            if not name:
                errors[CONF_NAME] = "name_required"
            elif any(r[CONF_NAME] == name for r in self._rooms):
                errors[CONF_NAME] = "duplicate_name"
            elif not climate or self.hass.states.get(climate) is None:
                errors[CONF_CLIMATE] = "entity_missing"
            elif not temp_sensor or self.hass.states.get(temp_sensor) is None:
                errors[CONF_TEMP_SENSOR] = "entity_missing"
            if not errors:
                room = {
                    CONF_NAME: name,
                    CONF_CLIMATE: climate,
                    CONF_TEMP_SENSOR: temp_sensor,
                    CONF_MAX_OPENING_NUMBER: user_input.get(CONF_MAX_OPENING_NUMBER, "") or "",
                    CONF_OPENING_SENSOR: user_input.get(CONF_OPENING_SENSOR, "") or "",
                }
                self._rooms.append(room)
                if not user_input.get("add_another", False):
                    return self.async_create_entry(
                        title=f"Hydraulik-Abgleich ({len(self._rooms)} Räume)",
                        data={"rooms": self._rooms},
                        options=self._opts,
                    )
                # fall through to a fresh form for the next room
                user_input = None
                errors = {}
        # after an error keep the entered values; after a successful add show an empty form
        schema = _room_schema(user_input if errors else None)
        return self.async_show_form(step_id="room", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Edit the options; rooms stay as configured."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = {**OPTION_DEFAULTS, **(self.config_entry.options or {})}
        return self.async_show_form(step_id="init", data_schema=_options_schema(defaults))
