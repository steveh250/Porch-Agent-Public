import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from porch_light.situation import (
    WeatherClient,
    WeatherError,
    summarise_owm,
    sun_context,
    twilight_phase,
)

LONDON = ZoneInfo("Europe/London")
OWM_RAIN = {
    "weather": [{"id": 501, "main": "Rain", "description": "moderate rain"}],
    "main": {"temp": 7.5}, "clouds": {"all": 90}, "visibility": 6000, "rain": {"1h": 2.1},
}


@pytest.mark.parametrize("elevation,phase", [
    (45, "day"), (0, "day"), (-0.833, "day"), (-1, "civil_twilight"),
    (-6, "civil_twilight"), (-6.01, "night"), (-30, "night"),
])
def test_twilight_phase(elevation, phase):
    assert twilight_phase(elevation) == phase


def test_sun_context_midday_and_night():
    noon = sun_context(51.5, -0.14, LONDON, datetime(2026, 6, 21, 12, 0, tzinfo=LONDON))
    assert noon["phase"] == "day" and noon["sun_elevation_deg"] > 50
    night = sun_context(51.5, -0.14, LONDON, datetime(2026, 11, 20, 22, 0, tzinfo=LONDON))
    assert night["phase"] == "night" and night["sun_elevation_deg"] < -30
    assert night["local_time"].startswith("2026-11-20T22:00")
    assert night["civil_dusk"] > night["sunset"]


def test_sun_context_converts_to_home_timezone():
    ctx = sun_context(51.5, -0.14, LONDON, datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc))
    assert ctx["local_time"].startswith("2026-07-01T21:00")  # BST


def test_sun_context_polar_night_has_no_times():
    ctx = sun_context(78.2, 15.6, ZoneInfo("Arctic/Longyearbyen"),
                      datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc))
    assert ctx["phase"] == "night" and "sunset" not in ctx


def test_summarise_owm():
    s = summarise_owm(OWM_RAIN)
    assert s["is_rain"] and not s["is_fog"] and not s["is_snow"]
    assert s["rain_1h_mm"] == 2.1 and s["cloud_cover_pct"] == 90
    assert summarise_owm({"weather": [{"id": 741, "main": "Fog"}]})["is_fog"]
    assert summarise_owm({"weather": [{"id": 601, "main": "Snow"}]})["is_snow"]
    assert not any(summarise_owm({"weather": [{"id": 800}]})[k] for k in ("is_rain", "is_snow", "is_fog"))


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def client_with(handler, clock, **kw):
    return WeatherClient("KEY", 51.5, -0.14, ttl_s=600, clock=clock,
                         transport=httpx.MockTransport(handler), **kw)


def test_weather_cached_for_ten_minutes():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=OWM_RAIN)

    clock = Clock()
    weather = client_with(handler, clock)
    first = asyncio.run(weather.current())
    clock.now += 599
    second = asyncio.run(weather.current())
    clock.now += 2
    third = asyncio.run(weather.current())

    assert len(requests) == 2
    assert (first["cached"], second["cached"], third["cached"]) == (False, True, False)
    q = requests[0].url.params
    assert (q["lat"], q["lon"], q["units"]) == ("51.5", "-0.14", "metric")


def test_weather_cache_survives_restart(tmp_path):
    cache = tmp_path / "weather.json"
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=OWM_RAIN)

    clock = Clock()
    asyncio.run(client_with(handler, clock, cache_file=cache).current())
    clock.now += 60
    again = asyncio.run(client_with(handler, clock, cache_file=cache).current())
    assert len(calls) == 1 and again["cached"] and again["condition"] == "Rain"
    assert "KEY" not in cache.read_text()


def test_weather_http_error_does_not_leak_key():
    weather = client_with(lambda r: httpx.Response(401, json={}), Clock())
    with pytest.raises(WeatherError) as info:
        asyncio.run(weather.current())
    assert "401" in str(info.value) and "KEY" not in str(info.value)


def test_weather_network_error():
    def handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(WeatherError):
        asyncio.run(client_with(handler, Clock()).current())


def test_weather_without_key():
    with pytest.raises(WeatherError, match="OWM_API_KEY"):
        asyncio.run(WeatherClient(None, 51.5, -0.14).current())
