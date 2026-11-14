"""Pure balancing logic: heat-up rate fitting and opening-degree adjustment.

This module deliberately contains no Home Assistant imports so it can be
unit-tested standalone (pytest) and reused elsewhere.

Idea — iterative cycle learning (eigene Entwicklung):
1. All rooms heat at the same time, each valve limited to its current
   maximum opening degree.
2. The heat-up rate (K/h) of each room is measured while heating.
3. Rooms that heat faster than the median are throttled, rooms that heat
   slower are opened up (damped, step-limited, iterated over cycles).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# A rate fit needs at least this many samples and a minimum time spread,
# otherwise the slope is dominated by sensor noise.
MIN_SAMPLES = 4
MIN_SPREAD_MINUTES = 10.0

DEFAULT_TOLERANCE = 0.15
DEFAULT_STEP_CAP = 15
DEFAULT_MIN_OPENING = 20
DEFAULT_MAX_OPENING = 100

# Damping exponent for the proportional rule: new = cur * (median/rate)^DAMPING.
# 1.0 would be a direct proportional correction; 0.5 converges more calmly.
DAMPING = 0.5


@dataclass
class Sample:
    """One temperature sample during a measurement cycle."""

    minutes: float
    temperature: float


def compute_rate_k_per_h(samples: list[Sample]) -> float | None:
    """Least-squares slope of the temperature samples, in K per hour.

    Returns None when there is not enough data for a meaningful fit.
    """
    pts = [
        (s.minutes, s.temperature)
        for s in samples
        if s.temperature is not None and s.minutes is not None
    ]
    if len(pts) < MIN_SAMPLES:
        return None
    t0 = min(p[0] for p in pts)
    t1 = max(p[0] for p in pts)
    if t1 - t0 < MIN_SPREAD_MINUTES:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    num = sum((p[0] - mx) * (p[1] - my) for p in pts)
    den = sum((p[0] - mx) ** 2 for p in pts)
    if den <= 0:
        return None
    slope_k_per_min = num / den
    return slope_k_per_min * 60.0


def estimate_rate_kph(samples: list[Sample]) -> float | None:
    """Best-effort heat-up rate: LSQ fit when there is enough data, else mean rise.

    The fallback matters for rooms that reach their target very fast: the fit
    rejects short windows (noise), but a room that arrives early is by
    definition one of the fast ones and must not be treated as "no data".
    """
    fit = compute_rate_k_per_h(samples)
    if fit is not None:
        return fit
    if len(samples) < 2:
        return None
    first, last = samples[0], samples[-1]
    span = last.minutes - first.minutes
    if span <= 0:
        return None
    return (last.temperature - first.temperature) / (span / 60.0)


@dataclass
class RoomMeasure:
    """Measurement result of one room in one cycle."""

    name: str
    rate_kph: float | None  # measured heat-up rate (K/h), None = no usable data
    reached_target: bool  # whether the room reached its target in time
    current_max: int  # opening degree the valve was limited to during the run


@dataclass
class RoomAdjustment:
    """Resulting decision for one room."""

    name: str
    old_max: int
    new_max: int
    reason: str
    stuck: bool = False  # system limit reached (100 % and still never arrives)
    unchanged: bool = False


@dataclass
class AdjustResult:
    adjustments: list[RoomAdjustment]
    converged: bool
    median_rate: float | None
    advice: list[str] = field(default_factory=list)


def adjust_openings(
    measures: list[RoomMeasure],
    min_opening: int = DEFAULT_MIN_OPENING,
    tolerance: float = DEFAULT_TOLERANCE,
    step_cap: int = DEFAULT_STEP_CAP,
) -> AdjustResult:
    """Compare rates against the median and derive new opening degrees.

    - Faster rooms get their maximum opening degree reduced.
    - Slower rooms get it increased (capped at 100 %).
    - Rooms already at 100 % that never reach the target are flagged "stuck":
      the system cannot help them digitally, the report says so.
    - Changes are damped (DAMPING) and limited to step_cap points per cycle.
    - Converged: every measured room is within the tolerance band of the
      median and no room is stuck.
    """
    advice: list[str] = []
    adjustments: list[RoomAdjustment] = []

    if not measures:
        return AdjustResult([], True, None, ["Keine Räume konfiguriert."])

    if len(measures) == 1:
        m = measures[0]
        adjustments.append(
            RoomAdjustment(
                m.name,
                m.current_max,
                m.current_max,
                "Nur ein Raum konfiguriert — nichts zu verteilen.",
                unchanged=True,
            )
        )
        return AdjustResult(adjustments, True, m.rate_kph, advice)

    valid = [m for m in measures if m.rate_kph is not None and m.rate_kph > 0]
    median = statistics.median([m.rate_kph for m in valid]) if valid else None

    converged = True

    for m in measures:
        cur = m.current_max
        if m.rate_kph is None or m.rate_kph <= 0:
            converged = False
            if cur >= DEFAULT_MAX_OPENING:
                adjustments.append(
                    RoomAdjustment(
                        m.name,
                        cur,
                        cur,
                        "Keine Aufheizung messbar trotz 100 % Öffnungsgrad — "
                        "Vorlauf/Hydraulik dieses Kreises prüfen.",
                        stuck=True,
                        unchanged=True,
                    )
                )
                advice.append(
                    f"{m.name}: heizt auch bei voller Öffnung nicht — "
                    "Rücklaufverschraubung zu, Luft im Kreis oder Vorlauf zu kalt."
                )
            else:
                new = max(min_opening, min(DEFAULT_MAX_OPENING, cur + step_cap))
                adjustments.append(
                    RoomAdjustment(
                        m.name,
                        cur,
                        new,
                        "Keine brauchbaren Messdaten — Öffnungsgrad erhöht für "
                        "den nächsten Zyklus.",
                    )
                )
            continue

        ratio = median / m.rate_kph if median else 1.0
        if abs(ratio - 1.0) <= tolerance:
            if cur < min_opening:
                adjustments.append(
                    RoomAdjustment(
                        m.name,
                        cur,
                        min_opening,
                        f"heizt im Toleranzband, aber Öffnungsgrad {cur} % liegt "
                        f"unter der Untergrenze → auf {min_opening} % angehoben.",
                    )
                )
            else:
                adjustments.append(
                    RoomAdjustment(
                        m.name,
                        cur,
                        cur,
                        f"heizt {m.rate_kph:.2f} K/h, im Toleranzband um den Median "
                        f"({median:.2f} K/h) — unverändert.",
                        unchanged=True,
                    )
                )
            continue

        converged = False
        target = cur * (ratio**DAMPING)
        new = int(round(target))
        # limit the step per cycle
        new = min(cur + step_cap, max(cur - step_cap, new))
        # clamp to the valid range
        clamped = False
        if new < min_opening:
            new = min_opening
            clamped = True
        if new > DEFAULT_MAX_OPENING:
            new = DEFAULT_MAX_OPENING
            clamped = True

        direction = "schneller" if ratio < 1.0 else "langsamer"
        reason = (
            f"heizt {m.rate_kph:.2f} K/h ({ratio:.2f}× {direction} als Median "
            f"{median:.2f} K/h) → Öffnungsgrad {cur} → {new} %."
        )
        stuck_flag = False
        if clamped:
            if new <= min_opening and ratio < 1.0:
                reason += " (an Untergrenze)"
                advice.append(
                    f"{m.name}: liegt an der Untergrenze {min_opening} %. "
                    "Für eine stärkere Drosselung muss die Rücklaufverschraubung "
                    "vorgenommen werden."
                )
            if new >= DEFAULT_MAX_OPENING and ratio > 1.0:
                reason += " (an Obergrenze 100 %)"
                stuck_flag = True
                advice.append(
                    f"{m.name}: braucht mehr, als das Ventil hergibt — der Kreis ist "
                    "vermutlich zu schwach versorgt (Rücklaufverschraubung öffnen, "
                    "Luft entlüften oder Heizlast prüfen)."
                )
        adjustments.append(RoomAdjustment(m.name, cur, new, reason, stuck=stuck_flag))

    return AdjustResult(adjustments, converged, median, advice)


# ------------------------------------------------------------ efficiency (ΔT)

def evaluate_delta_t(
    flow_temp: float | None,
    return_temp: float | None,
    low: float = 5.0,
    high: float = 15.0,
) -> dict | None:
    """Bewerte die Spreizung (ΔT) zwischen Vorlauf und Rücklauf.

    - Spreizung zu klein: Rücklauf zu warm — der Wärmeerzeuger kann bei
      Brennwerttechnik nicht kondensieren und der Kreis strömt über.
    - Spreizung zu groß: zu wenig Durchfluss — Heizflächen werden nicht
      voll durchspült.
    Rückgabe: {"delta": K, "status": str, "advice": str} oder None ohne Daten.
    """
    if flow_temp is None or return_temp is None:
        return None
    delta = round(flow_temp - return_temp, 2)
    if delta < 0:
        return {
            "delta": delta,
            "status": "fehler",
            "advice": "Rücklauf ist wärmer als Vorlauf — Sensor-Tausch oder Messstelle prüfen.",
        }
    if delta < 1.0:
        return {
            "delta": delta,
            "status": "keine_spreizung",
            "advice": "Kaum Spreizung — Heizbetrieb/Heizpumpe prüfen oder es strömt ungenutzt durch.",
        }
    if delta < low:
        return {
            "delta": delta,
            "status": "spreizung_zu_klein",
            "advice": "Spreizung zu klein: Rücklauf ist zu warm — Vorlauf senken (Brennwertgewinn) "
            "oder Durchfluss drosseln.",
        }
    if delta > high:
        return {
            "delta": delta,
            "status": "spreizung_zu_gross",
            "advice": "Spreizung sehr groß: wenig Durchfluss — Heizflächen werden nicht voll "
            "durchspült; Öffnungsgrade/Volumenstrom erhöhen.",
        }
    return {
        "delta": delta,
        "status": "ok",
        "advice": f"Spreizung {delta:.1f} K — gesunder Betrieb.",
    }
