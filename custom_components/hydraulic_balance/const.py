"""Constants for the Hydraulic Balance integration."""
from __future__ import annotations

DOMAIN = "hydraulic_balance"
MANUFACTURER = "LichtValley"

# Room config keys (entry.data["rooms"] is a list of these dicts)
CONF_ROOMS = "rooms"
CONF_NAME = "name"
CONF_CLIMATE = "climate"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_MAX_OPENING_NUMBER = "max_opening_number"
CONF_OPENING_SENSOR = "opening_sensor"

# Option keys
CONF_INTERVAL = "measure_interval_s"
CONF_BOOST_DELTA = "boost_delta"
CONF_BOOST_CAP = "boost_cap"
CONF_MIN_OPENING = "min_opening"
CONF_TIMEOUT = "timeout_min"
CONF_TOLERANCE = "tolerance"
CONF_AUTOSET = "autoset"
CONF_MAX_CYCLES = "max_cycles"
CONF_FLOW_SENSOR = "flow_sensor"
CONF_RETURN_SENSOR = "return_sensor"

OPTION_DEFAULTS: dict[str, float] = {
    CONF_INTERVAL: 60,
    CONF_BOOST_DELTA: 3.0,
    CONF_BOOST_CAP: 24.0,
    CONF_MIN_OPENING: 20,
    CONF_TIMEOUT: 90,
    CONF_TOLERANCE: 15,
    CONF_AUTOSET: True,
    CONF_MAX_CYCLES: 4,
    CONF_FLOW_SENSOR: "",
    CONF_RETURN_SENSOR: "",
}
