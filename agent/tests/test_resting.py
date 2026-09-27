"""The resting light: low from dusk to dawn, off in daylight, back to resting after arrivals."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from porch_light.arrival import ArrivalDeps
from porch_light.config import ConfigError, load_config
from porch_light.guardrails import Decision
from porch_light.lightctl import FakeLight
from porch_light.resting import RestingLight

# London (the example config's home), 20 Nov 2026.
NIGHT = datetime(2026, 11, 20, 21, 30, tzinfo=timezone.utc)
NOON = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
DUSK_BEFORE = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)   # sun just set, above -6
DUSK_AFTER = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)    # below -6


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make(config, light=None, now=NIGHT):
    light = light or FakeLight()
    clock = Clock(now)
    deps = ArrivalDeps(light_factory=lambda: light, weather=None, clock=clock)
    return RestingLight(config, deps, clock=clock), light, clock


def run(coro):
    return asyncio.run(coro)


def test_night_rests_low_and_warm(config):
    resting, light, _ = make(config)
    applied = run(resting.tick())
    assert applied == Decision("on", 20, 2700)
    assert (light.on, light.brightness_pct, light.color_temp_kelvin) == (True, 20, 2700)


def test_daylight_is_off(config):
    resting, light, _ = make(config, now=NOON)
    light.on = True
    assert run(resting.tick()) == Decision("off")
    assert light.on is False


def test_only_acts_on_change(config):
    resting, light, _ = make(config)
    run(resting.tick())
    writes = len([c for c in light.calls if c[0] != "get_light_state"])
    assert run(resting.tick()) is None
    assert len([c for c in light.calls if c[0] != "get_light_state"]) == writes


def test_manual_change_is_left_alone_until_the_next_transition(config):
    resting, light, clock = make(config, now=DUSK_AFTER)
    run(resting.tick())
    run(light.turn_on(100, 4000))          # someone sets it by hand
    clock.now = NIGHT
    assert run(resting.tick()) is None     # still night: no change
    assert light.brightness_pct == 100
    clock.now = NOON + timedelta(days=1)   # dawn has passed
    assert run(resting.tick()) == Decision("off")


def test_switches_on_at_civil_dusk(config):
    resting, light, clock = make(config, now=DUSK_BEFORE)
    assert run(resting.tick()) == Decision("off")
    clock.now = DUSK_AFTER
    assert run(resting.tick()) == Decision("on", 20, 2700)


def test_reverts_ten_minutes_after_an_arrival(config):
    resting, light, clock = make(config)
    run(resting.tick())
    run(light.turn_on(85, 2700))            # the agent brightened it
    resting.note_arrival({"decision": {"action": "on", "brightness_pct": 85}}, clock.now)
    clock.now += timedelta(minutes=9, seconds=59)
    assert run(resting.tick()) is None and light.brightness_pct == 85
    clock.now += timedelta(seconds=1)
    assert run(resting.tick()) == Decision("on", 20, 2700)
    assert light.brightness_pct == 20


def test_arrival_that_changed_nothing_is_not_reverted(config):
    resting, light, clock = make(config, now=NOON)
    run(resting.tick())
    resting.note_arrival({"decision": {"action": "none"}}, clock.now)
    assert resting.hold_until is None
    clock.now += timedelta(minutes=11)
    assert run(resting.tick()) is None


def test_second_arrival_extends_the_hold(config):
    resting, light, clock = make(config)
    resting.note_arrival({"decision": {"action": "on"}}, clock.now)
    clock.now += timedelta(minutes=8)
    resting.note_arrival({"decision": {"action": "on"}}, clock.now)
    clock.now += timedelta(minutes=8)
    assert run(resting.tick()) is None
    clock.now += timedelta(minutes=2)
    assert run(resting.tick()) is not None


def test_revert_after_dawn_goes_to_off(config):
    resting, light, clock = make(config, now=NIGHT)
    run(light.turn_on(80, 2700))
    resting.note_arrival({"decision": {"action": "on"}}, clock.now)
    clock.now = NOON + timedelta(days=1)
    assert run(resting.tick()) == Decision("off") and light.on is False


def test_unreachable_bulb_is_retried(config):
    light = FakeLight(reachable=False)
    resting, light, _ = make(config, light)
    assert run(resting.tick()) is None
    light.reachable = True
    assert run(resting.tick()) == Decision("on", 20, 2700)


def test_values_are_clamped(config):
    config = replace(config, resting=replace(config.resting, brightness_pct=2, color_temp_kelvin=1500))
    resting, light, _ = make(config)
    assert run(resting.tick()) == Decision("on", 10, 2200)


def test_waits_for_an_arrival_in_progress(config):
    resting, light, _ = make(config)

    async def main():
        async with resting.lock:                  # an arrival is running
            task = asyncio.create_task(resting.tick())
            await asyncio.sleep(0.01)
            assert not task.done() and light.on is False
        return await task

    assert run(main()) == Decision("on", 20, 2700)


def test_config_defaults_and_validation(config, tmp_path):
    r = config.resting
    assert (r.enabled, r.brightness_pct, r.color_temp_kelvin, r.revert_after_min) == (True, 20, 2700, 10)
    (tmp_path / "c.yaml").write_text("home: {lat: 1, lon: 2}\nresting: {enabled: maybe}\n")
    with pytest.raises(ConfigError, match="resting.enabled"):
        load_config(tmp_path / "c.yaml", env_file=tmp_path / "none")
    (tmp_path / "c.yaml").write_text("home: {lat: 1, lon: 2}\nresting: {enabled: false}\n")
    assert load_config(tmp_path / "c.yaml", env_file=tmp_path / "none").resting.enabled is False
