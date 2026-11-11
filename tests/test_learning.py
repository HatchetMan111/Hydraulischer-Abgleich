"""Tests for the pure balancing logic (no Home Assistant required)."""

from __future__ import annotations

import importlib.util
import pathlib
import statistics
import sys

import pytest

# load learning.py directly from its file path: the package __init__ pulls in
# Home Assistant, which is not installed in this test environment
_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hydraulic_balance"
    / "learning.py"
)
_spec = importlib.util.spec_from_file_location("hb_learning", _MODULE_PATH)
_learning = importlib.util.module_from_spec(_spec)
sys.modules["hb_learning"] = _learning
_spec.loader.exec_module(_learning)

DEFAULT_STEP_CAP = _learning.DEFAULT_STEP_CAP
Sample = _learning.Sample
RoomMeasure = _learning.RoomMeasure
adjust_openings = _learning.adjust_openings
compute_rate_k_per_h = _learning.compute_rate_k_per_h


def rate_list(result):
    return [a for a in result.adjustments]


# ---------------------------------------------------------------- rate fit

def test_rate_fit_linear():
    # 5 K per hour = 0.0833 K per minute
    samples = [Sample(minutes=m, temperature=20.0 + m * 5.0 / 60.0) for m in range(0, 61, 10)]
    rate = compute_rate_k_per_h(samples)
    assert rate is not None
    assert rate == pytest.approx(5.0, abs=0.01)


def test_rate_fit_noisy_is_stable():
    samples = [
        Sample(0, 20.0),
        Sample(10, 20.9),
        Sample(20, 21.6),
        Sample(30, 22.5),
        Sample(40, 23.3),
    ]
    rate = compute_rate_k_per_h(samples)
    assert rate == pytest.approx(5.0, abs=0.2)


def test_rate_fit_insufficient_samples():
    assert compute_rate_k_per_h([Sample(0, 20), Sample(5, 20.4)]) is None


def test_rate_fit_insufficient_spread():
    samples = [Sample(m, 20.0 + m / 60.0) for m in (0, 2, 4, 6)]
    assert compute_rate_k_per_h(samples) is None


def test_rate_fit_flat():
    samples = [Sample(m, 21.0) for m in range(0, 61, 15)]
    rate = compute_rate_k_per_h(samples)
    assert rate == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------- adjust

def _measure(name, rate, reached=True, cur=100):
    return RoomMeasure(name=name, rate_kph=rate, reached_target=reached, current_max=cur)


def test_single_room_is_trivially_balanced():
    result = adjust_openings([_measure("Wohnzimmer", 3.0)])
    assert result.converged
    assert result.adjustments[0].unchanged


def test_fast_room_throttled_slow_room_capped():
    # fast: 3.0, median 2.0, slow: 1.0 — slow is already at 100 % and stuck
    result = adjust_openings([_measure("fast", 3.0), _measure("slow", 1.0)])
    by_name = {a.name: a for a in result.adjustments}

    assert not result.converged
    assert by_name["fast"].new_max < by_name["fast"].old_max
    assert by_name["fast"].old_max == 100
    assert by_name["slow"].stuck
    assert by_name["slow"].new_max == 100
    assert any("Rücklaufverschraubung" in a for a in result.advice)


def test_converged_when_all_within_tolerance():
    result = adjust_openings([_measure("a", 2.0), _measure("b", 2.1), _measure("c", 1.9)])
    assert result.converged
    assert all(a.unchanged for a in result.adjustments)


def test_step_cap_limits_change():
    # rates 2.0 / 0.6 → median 1.3; a is faster: proportional target ≈ 81,
    # but the step per cycle is limited to 100 - 15 = 85
    result = adjust_openings([_measure("a", 2.0), _measure("b", 0.6)], min_opening=20)
    by_name = {x.name: x for x in result.adjustments}
    assert by_name["a"].new_max == 85
    # b heats slower and is already at 100 % → cannot go higher, stays at 100
    assert by_name["b"].new_max == 100
    assert by_name["b"].stuck


def test_min_opening_clamp_and_advice():
    # fast room at a low opening already: 24 %, heating 4x faster than median
    # damped target = 24 * sqrt(1/4) ≈ 19 → clamped to min_opening 20
    result = adjust_openings(
        [_measure("fast", 4.0, cur=24), _measure("ref", 1.0, cur=100)], min_opening=20
    )
    by_name = {a.name: a for a in result.adjustments}
    assert by_name["fast"].new_max == 20
    assert any("Untergrenze" in a for a in result.advice)


def test_missing_rate_raises_opening():
    result = adjust_openings([_measure("a", None, cur=60), _measure("b", 2.0)])
    a = {x.name: x for x in result.adjustments}["a"]
    assert not result.converged
    assert a.new_max == 60 + DEFAULT_STEP_CAP


def test_no_measures():
    result = adjust_openings([])
    assert result.converged
    assert result.adjustments == []


def test_missing_rate_raise_respects_min_opening():
    result = adjust_openings([_measure("a", None, cur=0), _measure("b", 2.0)], min_opening=20)
    a = {x.name: x for x in result.adjustments}["a"]
    assert a.new_max == 20


# ---------------------------------------------------------------- estimate

def test_estimate_uses_fit_when_possible():
    samples = [Sample(m, 20.0 + m * 3.0 / 60.0) for m in range(0, 61, 10)]
    assert _learning.estimate_rate_kph(samples) == pytest.approx(3.0, abs=0.01)


def test_estimate_fallback_for_early_arriver():
    # room reaches the target after 8 minutes: the fit refuses (spread < 10 min),
    # but the fallback must deliver the average rise rate
    samples = [Sample(m, 20.0 + m * (22.5 / 60.0)) for m in range(0, 9)]
    rate = _learning.estimate_rate_kph(samples)
    assert rate == pytest.approx(22.5, abs=0.01)


def test_estimate_none_without_data():
    assert _learning.estimate_rate_kph([]) is None
    assert _learning.estimate_rate_kph([Sample(0, 20.0)]) is None
    assert _learning.estimate_rate_kph([Sample(0, 20.0), Sample(0, 20.1)]) is None


def test_early_arriver_is_throttled_not_raised():
    # regression: fast rooms that reach the target early used to end up with
    # no rate (fit spread too small) and were wrongly opened up
    result = adjust_openings([_measure("schnell", 22.5, cur=100), _measure("normal", 3.0, cur=100)])
    fast = {a.name: a for a in result.adjustments}["schnell"]
    assert fast.new_max < 100


def test_openings_stay_within_bounds_matrix():
    rates = [0.5, 1.0, 2.0, 4.0, 8.0]
    curs = [0, 5, 20, 37, 50, 77, 100]
    for rate in rates:
        for cur in curs:
            result = adjust_openings(
                [_measure("x", rate, cur=cur), _measure("y", 2.0, cur=50)], min_opening=20
            )
            for adj in result.adjustments:
                assert 20 <= adj.new_max <= 100, (rate, cur, adj)


# ---------------------------------------------------------------- simulation

def test_closed_loop_simulation_converges():
    """Simulate rooms with rate = base * opening/100 and run cycles until converged."""

    base = {"Wohnzimmer": 2.0, "Schlafzimmer": 2.5, "Kinderzimmer": 1.8}
    opening = {name: 100 for name in base}
    converged_cycle = None

    for cycle in range(1, 9):
        rates = {name: base[name] * opening[name] / 100.0 for name in base}
        measures = [
            RoomMeasure(name=name, rate_kph=rates[name], reached_target=True, current_max=opening[name])
            for name in base
        ]
        result = adjust_openings(measures, min_opening=20, tolerance=0.15, step_cap=15)
        for adj in result.adjustments:
            opening[adj.name] = adj.new_max
        if result.converged:
            converged_cycle = cycle
            break

    assert converged_cycle is not None, f"not converged within 8 cycles: {opening}"
    rates = {name: base[name] * opening[name] / 100.0 for name in base}
    median = statistics.median(rates.values())
    for name, rate in rates.items():
        assert abs(rate / median - 1.0) <= 0.15, f"{name}: {rate} vs median {median}"


def test_simulation_flags_undersized_system():
    """A room that cannot reach the median even at 100 % must be flagged, not looped forever."""

    base = {"schnell": 3.0, "mittel": 2.0, "schwach": 1.0}
    opening = {name: 100 for name in base}
    stuck_seen = False

    for _ in range(6):
        rates = {name: base[name] * opening[name] / 100.0 for name in base}
        measures = [
            RoomMeasure(name=name, rate_kph=rates[name], reached_target=True, current_max=opening[name])
            for name in base
        ]
        result = adjust_openings(measures, min_opening=20, tolerance=0.15, step_cap=15)
        for adj in result.adjustments:
            assert adj.new_max <= 100
            opening[adj.name] = adj.new_max
            if adj.stuck:
                stuck_seen = True
        if result.converged:
            break

    assert stuck_seen, "the weak room should be reported as system limit"
