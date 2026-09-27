"""The deterministic wrapper: timeout, fallback, verification and the log."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from porch_light.agent import AgentError, AgentOutcome, ToolCallRecord
from porch_light.arrival import ArrivalDeps, handle_arrival
from porch_light.guardrails import Decision
from porch_light.lightctl import FakeLight, LightError
from porch_light.situation import WeatherError

# London, 20 Nov 2026. 21:30 UTC is well after civil dusk; 12:00 is midday.
NIGHT = datetime(2026, 11, 20, 21, 30, tzinfo=timezone.utc)
NOON = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
RAIN = {"condition": "Rain", "is_rain": True, "is_snow": False, "is_fog": False}


class StaticWeather:
    def __init__(self, data=RAIN, error=None):
        self.data, self.error, self.calls = data, error, 0

    async def current(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.data


def agent_that(decision=None, *, raises=None, sleeps=0.0, applies=True):
    """A stand-in for run_agent that optionally acts on the light it is given."""
    seen = {}

    async def runner(config, *, distance_m, sun, weather, kelvin_range):
        seen.update(distance_m=distance_m, sun=sun, weather=weather, kelvin_range=kelvin_range)
        if sleeps:
            await asyncio.sleep(sleeps)
        if raises:
            raise raises
        if applies and decision.action == "on":
            await runner.light.turn_on(decision.brightness_pct, decision.color_temp_kelvin)
        return AgentOutcome(decision, "Dark and wet, so bright warm light.", True,
                            [ToolCallRecord("turn_on", {}, {}, True)], "{}", 0.4)

    runner.seen = seen
    return runner


def deps_for(light, runner, weather=None, at=NIGHT):
    runner.light = light
    return ArrivalDeps(light_factory=lambda: light, weather=weather or StaticWeather(),
                       agent_runner=runner, clock=lambda: at)


def logged(config):
    lines = config.event_log.read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_agent_success_is_logged(config):
    light = FakeLight()
    runner = agent_that(Decision("on", 90, 2700))
    record = asyncio.run(handle_arrival(config, deps_for(light, runner), 390.4))

    assert record["decided_by"] == "agent" and record["fallback_used"] is False
    assert record["decision"] == {"action": "on", "brightness_pct": 90, "color_temp_kelvin": 2700}
    assert record["reason"] == "Dark and wet, so bright warm light."
    assert record["verification"]["ok"] is True
    assert record["situation"]["sun"]["phase"] == "night"
    assert record["situation"]["weather"] == RAIN
    assert runner.seen["kelvin_range"].min == 2200

    [line] = logged(config)
    for key in ("timestamp", "distance_m", "situation", "decision", "reason",
                "verification", "fallback_used"):
        assert key in line
    assert line["distance_m"] == 390.4


def test_timeout_falls_back_at_night(config):
    config = replace(config, llm=replace(config.llm, timeout_s=0.05))
    light = FakeLight()
    record = asyncio.run(handle_arrival(
        config, deps_for(light, agent_that(Decision("on", 50, 2700), sleeps=5)), 390))

    assert record["fallback_used"] is True and record["decided_by"] == "fallback"
    assert "timed out" in record["reason"]
    assert record["decision"] == {"action": "on", "brightness_pct": 80, "color_temp_kelvin": 2700}
    assert (light.on, light.brightness_pct, light.color_temp_kelvin) == (True, 80, 2700)
    assert record["verification"]["ok"] is True


@pytest.mark.parametrize("error", [AgentError("no JSON"), RuntimeError("HTTP 502 from OpenRouter")])
def test_agent_error_falls_back_at_night(config, error):
    light = FakeLight()
    record = asyncio.run(handle_arrival(config, deps_for(light, agent_that(raises=error)), 390))
    assert record["fallback_used"] and light.on and light.brightness_pct == 80


def test_agent_error_in_daylight_does_nothing(config):
    light = FakeLight()
    record = asyncio.run(handle_arrival(
        config, deps_for(light, agent_that(raises=RuntimeError("down")), at=NOON), 390))
    assert record["fallback_used"] and record["decision"]["action"] == "none"
    assert "before civil dusk" in record["reason"]
    assert [c for c in light.calls if c[0] != "get_light_state"] == []


def test_weather_failure_skips_agent_and_falls_back(config):
    light = FakeLight()
    runner = agent_that(Decision("on", 40, 3000))
    weather = StaticWeather(error=WeatherError("HTTP 401"))
    record = asyncio.run(handle_arrival(config, deps_for(light, runner, weather), 390))
    assert runner.seen == {}  # the LLM was never called
    assert record["fallback_used"] and "weather unavailable" in record["reason"]
    assert light.brightness_pct == 80


def test_force_fallback(config):
    light = FakeLight()
    weather = StaticWeather()
    record = asyncio.run(handle_arrival(
        config, deps_for(light, agent_that(Decision("off")), weather), 390, force_fallback=True))
    assert record["fallback_used"] and weather.calls == 0


def test_mismatch_is_reapplied_once(config):
    class Sticky(FakeLight):
        """Ignores the first turn_on, as a bulb dropping a UDP packet would."""
        dropped = False

        async def turn_on(self, brightness_pct=None, color_temp_kelvin=None):
            if not self.dropped:
                self.dropped = True
                self.calls.append(("turn_on (dropped)", {}))
                return self._doc()
            return await super().turn_on(brightness_pct, color_temp_kelvin)

    light = Sticky()
    record = asyncio.run(handle_arrival(config, deps_for(light, agent_that(Decision("on", 70, 2700))), 390))
    assert record["verification"]["reapplied"] is True
    assert record["verification"]["ok"] is True
    assert light.on and light.brightness_pct == 70


def test_agent_decision_not_applied_is_caught_by_verification(config):
    light = FakeLight()
    runner = agent_that(Decision("on", 60, 2700), applies=False)
    record = asyncio.run(handle_arrival(config, deps_for(light, runner), 390))
    assert record["verification"] == {**record["verification"], "ok": True, "reapplied": True}
    assert light.brightness_pct == 60


def test_unreachable_bulb_is_logged_not_raised(config):
    light = FakeLight(reachable=False)
    record = asyncio.run(handle_arrival(config, deps_for(light, agent_that(raises=RuntimeError("x"))), 390))
    assert record["verification"]["ok"] is False
    assert any("fallback apply" in e for e in record["errors"])
    assert len(logged(config)) == 1


def test_server_down_is_logged_not_raised(config):
    class Down:
        async def __aenter__(self):
            raise LightError("cannot reach MCP server")

        async def __aexit__(self, *a):
            return None

    runner = agent_that(raises=RuntimeError("mcp down too"))
    deps = ArrivalDeps(light_factory=Down, weather=StaticWeather(), agent_runner=runner,
                       clock=lambda: NIGHT)
    record = asyncio.run(handle_arrival(config, deps, 390))
    assert record["fallback_used"] and record["verification"]["ok"] is False
    assert any("read state before" in e for e in record["errors"])


def test_server_reported_kelvin_range_is_used_for_fallback(config):
    light = FakeLight(kelvin_range=(2700, 6500))
    config = replace(config, fallback=replace(config.fallback, color_temp_kelvin=2200))
    record = asyncio.run(handle_arrival(config, deps_for(light, agent_that(raises=RuntimeError())), 390))
    assert record["decision"]["color_temp_kelvin"] == 2700 and light.color_temp_kelvin == 2700


def test_log_appends(config):
    for _ in range(3):
        asyncio.run(handle_arrival(config, deps_for(FakeLight(), agent_that(Decision("none"))), 390))
    assert len(logged(config)) == 3


def test_transport_exception_group_is_unwrapped(config):
    class Refused:
        async def __aenter__(self):
            raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionRefusedError("refused")])

        async def __aexit__(self, *a):
            return None

    deps = ArrivalDeps(light_factory=Refused, weather=StaticWeather(),
                       agent_runner=agent_that(raises=RuntimeError()), clock=lambda: NIGHT)
    record = asyncio.run(handle_arrival(config, deps, 390))
    assert "ConnectionRefusedError: refused" in record["errors"][0]
