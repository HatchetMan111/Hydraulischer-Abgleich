"""End-to-end smoke tests for the coordinator, using a fake Home Assistant.

Simulates rooms with rate = base * opening/100 and accelerated time, so a
complete balancing run (several cycles, dozens of measurement intervals)
finishes in milliseconds.
"""

from __future__ import annotations

import asyncio
import contextlib

from custom_components.hydraulic_balance import coordinator as hb

ORIG_SETPOINT = 19.0

ROOMS = [
    {"name": "Wohnzimmer", "climate": "climate.wz", "temp_sensor": "sensor.wz",
     "max_opening_number": "number.wz", "opening_sensor": ""},
    {"name": "Schlafzimmer", "climate": "climate.sz", "temp_sensor": "sensor.sz",
     "max_opening_number": "number.sz", "opening_sensor": ""},
    {"name": "Kinderzimmer", "climate": "climate.kz", "temp_sensor": "sensor.kz",
     "max_opening_number": "number.kz", "opening_sensor": ""},
]

# K/h at 100 % opening: intentionally unequal to force real adjustments
BASE_RATES = {"sensor.wz": 4.0, "sensor.sz": 5.0, "sensor.kz": 3.6}


class RoomWorld:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = BASE_RATES[cfg["temp_sensor"]]
        self.temp = 20.0
        self.setpoint = ORIG_SETPOINT
        self.opening = 100.0

    def advance(self, seconds: float) -> None:
        if self.setpoint <= self.temp + 1e-9:
            return
        rate = self.base * self.opening / 100.0
        self.temp = min(self.setpoint, self.temp + rate * seconds / 3600.0)


class FakeState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class FakeServices:
    def __init__(self, hass):
        self.hass = hass
        self.calls = []

    def has_service(self, *args):
        return True

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, dict(data)))
        self.hass._apply(domain, service, data)


class FakeStates:
    def __init__(self, hass):
        self.hass = hass

    def get(self, entity_id):
        for world in self.hass.worlds:
            if world.cfg["temp_sensor"] == entity_id:
                return FakeState(f"{world.temp:.2f}")
            if world.cfg["climate"] == entity_id:
                return FakeState("heat", {"temperature": world.setpoint, "hvac_mode": "heat"})
            if world.cfg["max_opening_number"] == entity_id:
                return FakeState(f"{world.opening:.1f}")
        return None


class FakeHass:
    def __init__(self, worlds):
        self.worlds = worlds
        self._store_data = {}
        self.tasks = []
        self.services = FakeServices(self)
        self.states = FakeStates(self)

    def _apply(self, domain, service, data):
        entity_id = data["entity_id"]
        for world in self.worlds:
            if domain == "climate" and service == "set_temperature" and world.cfg["climate"] == entity_id:
                world.setpoint = float(data["temperature"])
            elif domain == "number" and service == "set_value" and world.cfg["max_opening_number"] == entity_id:
                world.opening = float(data["value"])

    def async_create_task(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


def _make_coord(options=None):
    worlds = [RoomWorld(cfg) for cfg in ROOMS]
    hass = FakeHass(worlds)

    class FakeEntry:
        pass

    entry = FakeEntry()
    entry.entry_id = "test"
    entry.data = {"rooms": ROOMS}
    entry.options = options or {}
    return hb.HydraulicBalanceCoordinator(hass, entry), hass, worlds


def _fake_wait(worlds):
    """Replace only the real waiting: the coordinator keeps its virtual clock."""

    async def fake_wait(seconds):
        for world in worlds:
            world.advance(seconds)
        await asyncio.sleep(0)

    return fake_wait


def _run(coroutine):
    return asyncio.run(coroutine)


def test_full_run_converges_and_writes_results():
    async def scenario():
        coord, hass, worlds = _make_coord()
        await coord.async_load()
        coord._wait = _fake_wait(worlds)
        assert await coord.async_start() is True
        for task in hass.tasks:
            await task
        return coord, hass

    coord, hass = _run(scenario())

    assert coord.phase == hb.PHASE_DONE
    assert coord.converged
    assert 1 < coord.cycles_done <= 4
    for value in coord.learning.values():
        assert 20 <= value <= 100
    # the learned openings were written to the valves (autoset default True)
    set_values = [
        data["value"] for d, s, data in hass.services.calls if (d, s) == ("number", "set_value")
    ]
    assert set_values, "no opening degree was ever written"
    # learning persisted
    stored = hass._store_data.get("hydraulic_balance_test.json")
    assert stored and stored["converged"] is True
    # no room timed out — the simulation has enough power
    # (a real timeout logs "Timeout nach …"; the precheck line just states the limit)
    timeout_lines = [line for line in coord.report if "Timeout nach" in line]
    assert not timeout_lines, timeout_lines
    # the report explains the adjustments
    assert any("Median" in line for line in coord.report)


def test_stop_restores_setpoints_and_openings():
    async def scenario():
        coord, hass, worlds = _make_coord()
        await coord.async_load()
        coord._wait = _fake_wait(worlds)
        await coord.async_start()
        task = hass.tasks[-1]
        # let the run reach the measuring phase and heat a little
        for _ in range(6):
            await asyncio.sleep(0)
        assert coord.phase == hb.PHASE_MEASURING
        assert any(
            c[1] == "set_temperature" and c[2]["temperature"] > ORIG_SETPOINT
            for c in hass.services.calls
        )
        await coord.async_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return coord, hass, worlds

    coord, hass, worlds = _run(scenario())

    assert coord.phase == hb.PHASE_ABORTED
    assert all(world.setpoint == ORIG_SETPOINT for world in worlds)
    assert all(world.opening == 100.0 for world in worlds)
    assert any("abgebrochen" in line for line in coord.report)


def test_missing_entities_abort_the_run():
    async def scenario():
        coord, hass, worlds = _make_coord()
        # point one thermostat at a non-existing entity
        hass.worlds[1].cfg = dict(hass.worlds[1].cfg, climate="climate.nirgendwo")
        await coord.async_load()
        coord._wait = _fake_wait(worlds)
        await coord.async_start()
        for task in hass.tasks:
            await task
        return coord, hass

    coord, hass = _run(scenario())

    assert coord.phase == hb.PHASE_ABORTED
    assert any("nicht gefunden" in line for line in coord.report)
