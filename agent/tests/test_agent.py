import asyncio
import json

import pytest
from agent_framework import Content

from porch_light.agent import (
    AgentError,
    ClampMiddleware,
    load_instructions,
    make_local_light_tools,
    parse_final,
    run_agent,
)
from porch_light.guardrails import Decision, KelvinRange
from porch_light.lightctl import FakeLight

from .conftest import ScriptedChatClient, call, tool_results

SUN = {"local_time": "2026-11-20T18:30+00:00", "sun_elevation_deg": -14.2, "phase": "night"}
WEATHER = {"condition": "Rain", "is_rain": True, "is_snow": False, "is_fog": False}
RANGE = KelvinRange(2200, 6500)


def final(action="on", b=90, k=2700, reason="Dark and raining, so bright warm light."):
    return Content.from_text(json.dumps(
        {"action": action, "brightness_pct": b, "color_temp_kelvin": k,
         "verified": True, "reason": reason}))


def run(config, script, light=None):
    light = light or FakeLight()
    client = ScriptedChatClient(script)
    outcome = asyncio.run(run_agent(
        config, distance_m=390, sun=SUN, weather=WEATHER, kelvin_range=RANGE,
        chat_client=client, light_tools=make_local_light_tools(light)))
    return outcome, client, light


def test_happy_path_set_then_verify(config):
    outcome, client, light = run(config, [
        [call("get_sun_context"), call("get_weather")],
        call("turn_on", brightness_pct=90, color_temp_kelvin=2700),
        call("get_light_state"),
        final(),
    ])
    assert outcome.decision == Decision("on", 90, 2700)
    assert outcome.reason.startswith("Dark and raining")
    assert outcome.self_verified is True
    assert (light.on, light.brightness_pct, light.color_temp_kelvin) == (True, 90, 2700)
    assert [c.tool for c in outcome.tool_calls] == ["turn_on", "get_light_state"]
    # The model only ever sees the situation tools and the three light tools.
    assert sorted(client.offered_tools[0]) == sorted(
        ["get_sun_context", "get_weather", "get_light_state", "turn_on", "turn_off"])


def test_situation_tools_return_snapshot(config):
    _, client, _ = run(config, [[call("get_sun_context"), call("get_weather")], final()])
    results = tool_results(client)
    assert any("-14.2" in r for r in results) and any("Rain" in r for r in results)


def test_out_of_range_values_are_clamped_before_the_bulb(config):
    outcome, _, light = run(config, [
        call("turn_on", brightness_pct=250, color_temp_kelvin=1500),
        final(b=250, k=1500),
    ])
    assert light.calls[0] == ("turn_on", {"brightness_pct": 100, "color_temp_kelvin": 2200})
    assert outcome.tool_calls[0].requested == {"brightness_pct": 250, "color_temp_kelvin": 1500}
    assert outcome.tool_calls[0].sent == {"brightness_pct": 100, "color_temp_kelvin": 2200}
    # The reported decision is clamped the same way.
    assert outcome.decision == Decision("on", 100, 2200)


def test_too_dim_is_raised_to_minimum(config):
    _, _, light = run(config, [call("turn_on", brightness_pct=1, color_temp_kelvin=2700), final(b=1)])
    assert light.brightness_pct == 10


def test_rgb_arguments_are_dropped(config):
    middleware = ClampMiddleware(config.bulb, RANGE)
    sent = middleware.sanitise("turn_on", {"red": 255, "green": 0, "blue": 0, "brightness_pct": 50})
    assert sent == {"brightness_pct": 50}


def test_unknown_tools_are_refused(config):
    middleware = ClampMiddleware(config.bulb, RANGE)
    assert middleware.sanitise("set_scene", {"name": "Party"}) is None
    assert middleware.sanitise("set_color", {}) is None


def test_retry_after_mismatch(config):
    light = FakeLight()
    outcome, _, _ = run(config, [
        call("turn_on", brightness_pct=70, color_temp_kelvin=2700),
        call("get_light_state"),
        call("turn_on", brightness_pct=70, color_temp_kelvin=2700),
        call("get_light_state"),
        final(b=70),
    ], light)
    assert [c.tool for c in outcome.tool_calls].count("turn_on") == 2


def test_bulb_error_is_reported_to_model_not_raised(config):
    light = FakeLight(reachable=False)
    outcome, client, _ = run(config, [
        call("turn_on", brightness_pct=70, color_temp_kelvin=2700),
        final(b=70, reason="Tried to switch on, but the bulb is unreachable."),
    ], light)
    assert outcome.tool_calls[0].ok is False
    assert "unreachable" in " ".join(tool_results(client))


def test_no_json_is_an_error(config):
    text = Content.from_text("I turned the light on for you!")
    with pytest.raises(AgentError):
        run(config, [text])


def test_daylight_none_decision(config):
    outcome, _, light = run(config, [final(action="none", b=None, k=None, reason="Daylight.")])
    assert outcome.decision == Decision("none") and light.calls == []


@pytest.mark.parametrize("text,expected", [
    ('{"action": "off", "reason": "x"}', {"action": "off", "reason": "x"}),
    ('```json\n{"action": "on"}\n```', {"action": "on"}),
    ('Done. {"action": "none"}', {"action": "none"}),
    ("no json here", None),
    ("{broken", None),
])
def test_parse_final(text, expected):
    assert parse_final(text) == expected


def test_instructions_include_policy_and_protocol(config):
    text = load_instructions(config.policy_file)
    assert "2700" in text and "Operating protocol" in text and '"reason"' in text


def test_missing_api_key_fails_fast(config):
    with pytest.raises(AgentError, match="OPENROUTER_API_KEY"):
        asyncio.run(run_agent(config, distance_m=390, sun=SUN, weather=WEATHER,
                              kelvin_range=RANGE, light_tools=[]))
