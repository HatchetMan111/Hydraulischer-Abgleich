"""Coordinator: runs the iterative measurement/adjustment cycles."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import (
    CONF_BOOST_CAP,
    CONF_BOOST_DELTA,
    CONF_CLIMATE,
    CONF_INTERVAL,
    CONF_MAX_CYCLES,
    CONF_MAX_OPENING_NUMBER,
    CONF_MIN_OPENING,
    CONF_NAME,
    CONF_TEMP_SENSOR,
    CONF_TIMEOUT,
    CONF_TOLERANCE,
    CONF_AUTOSET,
    DOMAIN,
    OPTION_DEFAULTS,
)
from .learning import (
    DEFAULT_MAX_OPENING,
    DEFAULT_STEP_CAP,
    RoomMeasure,
    Sample,
    adjust_openings,
    estimate_rate_kph,
)

_LOGGER = logging.getLogger(__name__)

PHASE_IDLE = "aus"
PHASE_PRECHECK = "vorbereitung"
PHASE_MEASURING = "messung"
PHASE_ADJUSTING = "auswertung"
PHASE_DONE = "fertig"
PHASE_ABORTED = "abgebrochen"

REPORT_MAX_LINES = 200


class RoomRuntime:
    """Mutable per-room state during a run."""

    __slots__ = ("cfg", "samples", "target", "rate", "reached", "status", "reason", "start_temp")

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.samples: list[Sample] = []
        self.target: float | None = None
        self.rate: float | None = None
        self.reached = False
        self.status = "wartet"
        self.reason = ""
        self.start_temp: float | None = None

    @property
    def name(self) -> str:
        return str(self.cfg[CONF_NAME])


class HydraulicBalanceCoordinator:
    """Owns the balancing run: precheck -> measure -> adjust -> repeat."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        # virtual clock, advanced by _sleep(); the actual waiting (_wait) is
        # injectable so tests can simulate hours in milliseconds
        self._clock = 0.0
        self._wait = asyncio.sleep
        self.rooms = [RoomRuntime(cfg) for cfg in entry.data.get("rooms", [])]
        self.phase = PHASE_IDLE
        self.cycles_done = 0
        self.converged = False
        self.report: list[str] = []
        self.advice: list[str] = []
        self.last_median: float | None = None
        # learned maximum opening per room name (percent, float internally)
        self.learning: dict[str, float] = {r.name: float(DEFAULT_MAX_OPENING) for r in self.rooms}

        self._task: asyncio.Task | None = None
        self._stop_requested = False
        self._listeners: list = []
        self._orig_setpoints: dict[str, float | None] = {}
        self._orig_openings: dict[str, float | None] = {}
        self._orig_hvac_modes: dict[str, str | None] = {}
        self._store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}.json")

    # ------------------------------------------------------------------ API

    def opt(self, key: str) -> Any:
        return self.entry.options.get(key, OPTION_DEFAULTS.get(key))

    def async_add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def async_remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        learning = data.get("learning", {})
        for room in self.rooms:
            if room.name in learning:
                self.learning[room.name] = float(learning[room.name])
        self.cycles_done = int(data.get("cycles_done", 0))
        self.converged = bool(data.get("converged", False))

    async def _persist(self) -> None:
        await self._store.async_save(
            {
                "learning": dict(self.learning),
                "cycles_done": self.cycles_done,
                "converged": self.converged,
            }
        )

    async def _sleep(self, seconds: float) -> None:
        """Advance the virtual clock, then wait (real time in production)."""
        self._clock += seconds
        await self._wait(seconds)

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def async_start(self) -> bool:
        if self.running():
            _LOGGER.info("Balance run already active, ignoring start")
            return False
        self._stop_requested = False
        self._task = self.hass.async_create_task(self._run())
        return True

    async def async_stop(self) -> None:
        if self.running():
            self._stop_requested = True
            self._task.cancel()

    async def async_reset_learning(self) -> None:
        self.learning = {r.name: float(DEFAULT_MAX_OPENING) for r in self.rooms}
        self.cycles_done = 0
        self.converged = False
        self.report = ["Lernwerte zurückgesetzt."]
        self.advice = []
        self.last_median = None
        await self._persist()
        self.notify()

    async def async_shutdown(self) -> None:
        await self.async_stop()

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _state_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def _set_setpoint(self, room: RoomRuntime, temperature: float) -> None:
        climate_id = room.cfg[CONF_CLIMATE]
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": climate_id, "temperature": round(temperature, 1)},
                blocking=True,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Setpoint for %s failed: %s", climate_id, err)

    async def _set_opening(self, room: RoomRuntime, value: float) -> None:
        number_id = room.cfg.get(CONF_MAX_OPENING_NUMBER)
        if not number_id:
            return
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": number_id, "value": int(round(value))},
                blocking=True,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Opening degree for %s failed: %s", number_id, err)

    def _log(self, line: str) -> None:
        _LOGGER.info("balance: %s", line)
        self.report.append(line)
        if len(self.report) > REPORT_MAX_LINES:
            self.report = self.report[-REPORT_MAX_LINES:]

    async def _restore_room(self, room: RoomRuntime) -> None:
        """Restore the room's original setpoint and hvac mode."""
        climate_id = room.cfg[CONF_CLIMATE]
        setpoint = self._orig_setpoints.get(climate_id)
        if setpoint is not None:
            await self._set_setpoint(room, setpoint)
        mode = self._orig_hvac_modes.get(climate_id)
        if mode is not None:
            try:
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": climate_id, "hvac_mode": mode},
                    blocking=True,
                )
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.warning("Hvac mode for %s failed: %s", climate_id, err)

    async def _restore_all_rooms(self) -> None:
        for room in self.rooms:
            await self._restore_room(room)

    async def _restore_all_openings(self) -> None:
        for room in self.rooms:
            number_id = room.cfg.get(CONF_MAX_OPENING_NUMBER)
            if not number_id:
                continue
            original = self._orig_openings.get(number_id)
            if original is not None:
                await self._set_opening(room, original)

    # ------------------------------------------------------------ main run

    async def _run(self) -> None:
        try:
            self._clock = 0.0
            await self._precheck()
            max_cycles = int(self.opt(CONF_MAX_CYCLES))
            for cycle in range(1, max_cycles + 1):
                self.cycles_done = cycle
                await self._measure_cycle(cycle)
                await self._adjust(cycle)
                if self.converged:
                    break
            await self._finish()
        except asyncio.CancelledError:
            self.phase = PHASE_ABORTED
            self._log("Lauf abgebrochen — Solltemperaturen und Öffnungsgrade werden zurückgesetzt.")
            await self._restore_all_rooms()
            await self._restore_all_openings()
        except Exception as err:  # noqa: BLE001 - keep HA alive, report instead
            _LOGGER.exception("Balance run failed")
            self.phase = PHASE_ABORTED
            self._log(f"Lauf mit Fehler beendet: {err}")
            await self._restore_all_rooms()
            await self._restore_all_openings()
        finally:
            self.notify()

    async def _precheck(self) -> None:
        self.phase = PHASE_PRECHECK
        self.notify()
        self.report = []
        self.advice = []
        for room in self.rooms:
            climate_id = room.cfg[CONF_CLIMATE]
            temp_id = room.cfg[CONF_TEMP_SENSOR]
            if self.hass.states.get(climate_id) is None:
                raise RuntimeError(f"Thermostat {climate_id} ({room.name}) nicht gefunden")
            if self.hass.states.get(temp_id) is None:
                raise RuntimeError(f"Temperatursensor {temp_id} ({room.name}) nicht gefunden")
            climate_state = self.hass.states.get(climate_id)
            raw = climate_state.attributes.get("temperature") if climate_state else None
            try:
                self._orig_setpoints[climate_id] = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                self._orig_setpoints[climate_id] = None
            raw_mode = climate_state.attributes.get("hvac_mode") if climate_state else None
            self._orig_hvac_modes[climate_id] = raw_mode if isinstance(raw_mode, str) else None
            number_id = room.cfg.get(CONF_MAX_OPENING_NUMBER)
            if number_id:
                self._orig_openings[number_id] = self._state_float(self.hass, number_id)
        self._log(
            f"Vorbereitung ok: {len(self.rooms)} Räume, "
            f"Intervall {int(self.opt(CONF_INTERVAL))} s, Timeout {int(self.opt(CONF_TIMEOUT))} min."
        )
        self._log("Wichtig: Fenster geschlossen, Heizbetrieb mit warmem Vorlauf während der Messung.")

    async def _measure_cycle(self, cycle: int) -> None:
        self.phase = PHASE_MEASURING
        self.notify()
        interval = max(30, int(self.opt(CONF_INTERVAL)))
        timeout_min = int(self.opt(CONF_TIMEOUT))
        boost_delta = float(self.opt(CONF_BOOST_DELTA))
        boost_cap = float(self.opt(CONF_BOOST_CAP))

        active: list[RoomRuntime] = []
        for room in self.rooms:
            current = self._state_float(self.hass, room.cfg[CONF_TEMP_SENSOR])
            if current is None:
                room.status = "sensorfehler"
                room.rate = None
                room.samples = []
                continue
            room.start_temp = current
            room.target = min(current + boost_delta, boost_cap)
            room.samples = [Sample(0.0, current)]
            room.rate = None
            room.reached = False
            room.status = f"heizt ({current:.1f} °C → Ziel {room.target:.1f} °C)"
            await self._set_opening(room, self.learning.get(room.name, DEFAULT_MAX_OPENING))
            await self._set_setpoint(room, room.target)
            active.append(room)
        self._log(f"Zyklus {cycle}: Messung läuft bei Öffnungsgraden "
                  + ", ".join(f"{r.name} {int(self.learning.get(r.name, 100))} %" for r in active)
                  + ".")

        started = self._clock
        while active:
            await self._sleep(interval)
            elapsed = (self._clock - started) / 60.0
            still_active: list[RoomRuntime] = []
            for room in active:
                current = self._state_float(self.hass, room.cfg[CONF_TEMP_SENSOR])
                if current is None:
                    still_active.append(room)
                    continue
                room.samples.append(Sample(elapsed, current))
                if current >= room.target:
                    room.rate = estimate_rate_kph(room.samples)
                    room.reached = True
                    room.status = (
                        f"fertig in {elapsed:.0f} min ({room.rate:.2f} K/h)"
                        if room.rate
                        else f"fertig in {elapsed:.0f} min (Rate unklar)"
                    )
                    await self._restore_room(room)
                    continue
                if current >= boost_cap + 2.0:
                    room.rate = estimate_rate_kph(room.samples)
                    room.status = "abgebrochen: Überhitzungsschutz"
                    await self._restore_room(room)
                    continue
                room.status = f"heizt ({current:.1f} °C / Ziel {room.target:.1f} °C)"
                still_active.append(room)
            active = still_active
            if elapsed >= timeout_min:
                for room in active:
                    room.rate = estimate_rate_kph(room.samples)
                    room.status = (
                        f"Timeout nach {timeout_min} min ({room.rate:.2f} K/h)"
                        if room.rate
                        else f"Timeout nach {timeout_min} min (zu wenig Daten)"
                    )
                    await self._restore_room(room)
                self._log(f"Zyklus {cycle}: Timeout — {len(active)} Raum/Räume kamen nicht ans Ziel.")
                break
            self.notify()

    async def _adjust(self, cycle: int) -> None:
        self.phase = PHASE_ADJUSTING
        self.notify()
        measures = [
            RoomMeasure(
                name=room.name,
                rate_kph=room.rate,
                reached_target=room.reached,
                current_max=int(self.learning.get(room.name, DEFAULT_MAX_OPENING)),
            )
            for room in self.rooms
        ]
        result = adjust_openings(
            measures,
            min_opening=int(self.opt(CONF_MIN_OPENING)),
            tolerance=float(self.opt(CONF_TOLERANCE)) / 100.0,
            step_cap=DEFAULT_STEP_CAP,
        )
        for adj in result.adjustments:
            self.learning[adj.name] = float(adj.new_max)
            room = next((r for r in self.rooms if r.name == adj.name), None)
            if room:
                room.reason = adj.reason
                if adj.stuck:
                    room.status = "grenze erreicht"
        self.last_median = result.median_rate
        self.converged = result.converged
        self.advice = result.advice
        summary = "konvergiert" if result.converged else "nächster Zyklus"
        self._log(
            f"Zyklus {cycle}: Median {result.median_rate:.2f} K/h, {summary}."
            if result.median_rate
            else f"Zyklus {cycle}: keine verwertbaren Raten, {summary}."
        )
        for adj in result.adjustments:
            self._log(f"  {adj.name}: {adj.reason}")
        await self._persist()
        self.notify()

    async def _finish(self) -> None:
        self.phase = PHASE_DONE
        autoset = bool(self.opt(CONF_AUTOSET))
        for room in self.rooms:
            learned = int(round(self.learning.get(room.name, DEFAULT_MAX_OPENING)))
            number_id = room.cfg.get(CONF_MAX_OPENING_NUMBER)
            if autoset and number_id:
                await self._set_opening(room, learned)
                room.status = f"übernommen: {learned} %"
            elif number_id:
                room.status = f"Empfehlung: {learned} % (nicht geschrieben)"
            else:
                room.status = f"Empfehlung: {learned} % (kein Number-Entity zugewiesen)"
        if not autoset:
            # the numbers were written during the measurement cycles — put the
            # originals back so "Empfehlung (nicht geschrieben)" is actually true
            await self._restore_all_openings()
        await self._restore_all_rooms()
        if self.converged:
            self._log("Fertig: alle Räume innerhalb des Toleranzbands — Abgleich abgeschlossen.")
        else:
            self._log(
                f"Fertig nach {self.cycles_done} Zyklen ohne volle Konvergenz — "
                "Empfehlungen/Hinweise siehe Sensoren."
            )
        await self._persist()
        self.notify()
