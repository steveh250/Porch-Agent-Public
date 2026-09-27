import pytest

from porch_light.config import BulbConfig, FallbackConfig
from porch_light.guardrails import (
    Decision,
    KelvinRange,
    clamp_brightness,
    clamp_kelvin,
    fallback_decision,
    parse_decision,
    resolve_kelvin_range,
    verify,
)

BULB = BulbConfig()  # 10-100 %, 2200-6500 K
RANGE = KelvinRange(2200, 6500)


@pytest.mark.parametrize("value,expected", [
    (0, 10), (5, 10), (10, 10), (55, 55), (100, 100), (150, 100), (-20, 10),
    (72.6, 73), ("80", 80), (None, None), ("bright", None), (True, None),
])
def test_clamp_brightness(value, expected):
    assert clamp_brightness(value, BULB) == expected


@pytest.mark.parametrize("value,expected", [
    (1000, 2200), (2200, 2200), (2700, 2700), (6500, 6500), (9000, 6500), (None, None),
])
def test_clamp_kelvin(value, expected):
    assert clamp_kelvin(value, RANGE) == expected


def test_kelvin_range_from_server_wins_when_narrower():
    doc = {"kelvin_range": {"min": 2700, "max": 6000}}
    assert resolve_kelvin_range(doc, BULB) == KelvinRange(2700, 6000)


def test_config_can_narrow_but_not_widen():
    warm_only = BulbConfig(kelvin_min=2200, kelvin_max=3000)
    assert resolve_kelvin_range({"kelvin_range": {"min": 2700, "max": 6500}}, warm_only) == KelvinRange(2700, 3000)
    wide = BulbConfig(kelvin_min=1000, kelvin_max=10000)
    assert resolve_kelvin_range({"kelvin_range": {"min": 2200, "max": 6500}}, wide) == KelvinRange(2200, 6500)


@pytest.mark.parametrize("doc", [None, {}, {"kelvin_range": None}, {"kelvin_range": {"min": "x"}}])
def test_kelvin_range_falls_back_to_config(doc):
    assert resolve_kelvin_range(doc, BULB) == RANGE


def test_kelvin_range_disjoint_trusts_bulb():
    cfg = BulbConfig(kelvin_min=7000, kelvin_max=8000)
    assert resolve_kelvin_range({"kelvin_range": {"min": 2200, "max": 6500}}, cfg) == RANGE


def test_decision_clamped():
    d = Decision("on", 500, 1500).clamped(BULB, RANGE)
    assert d == Decision("on", 100, 2200)
    assert Decision("off", 50, 2700).clamped(BULB, RANGE) == Decision("off")


@pytest.mark.parametrize("doc,expected", [
    ({"action": "on", "brightness_pct": 70, "color_temp_kelvin": 2700}, Decision("on", 70, 2700)),
    ({"action": "ON", "brightness_pct": "70"}, Decision("on", 70, None)),
    ({"action": "off", "brightness_pct": 70}, Decision("off")),
    ({"action": "none"}, Decision("none")),
    ({"action": "dim"}, None), ({}, None), (None, None), ("on", None),
])
def test_parse_decision(doc, expected):
    assert parse_decision(doc) == expected


def test_fallback_after_civil_dusk():
    assert fallback_decision({"sun_elevation_deg": -6.5}, FallbackConfig()) == Decision("on", 80, 2700)


@pytest.mark.parametrize("elevation", [30, 0, -3, -6.0])
def test_fallback_does_nothing_before_civil_dusk(elevation):
    assert fallback_decision({"sun_elevation_deg": elevation}, FallbackConfig()).action == "none"


def state(on=True, b=70, k=2700, reachable=True):
    return {"reachable": reachable, "state": {"on": on, "brightness_pct": b, "color_temp_kelvin": k}}


def test_verify_match_with_rounding_tolerance():
    assert verify(Decision("on", 70, 2700), state(b=69, k=2700)).ok


@pytest.mark.parametrize("doc,problem", [
    (state(on=False), "bulb is off"),
    (state(b=40), "brightness"),
    (state(k=6500), "colour temperature"),
    (state(reachable=False), "not reachable"),
    (None, "not reachable"),
])
def test_verify_mismatch(doc, problem):
    result = verify(Decision("on", 70, 2700), doc)
    assert not result.ok and problem in result.detail


def test_verify_off_and_none():
    assert verify(Decision("off"), state(on=False)).ok
    assert not verify(Decision("off"), state(on=True)).ok
    assert verify(Decision("none"), None).ok
