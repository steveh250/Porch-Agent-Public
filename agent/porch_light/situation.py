"""What the agent needs to know about the world: the sun and the weather."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from astral import Observer
from astral.sun import azimuth, elevation, sun

log = logging.getLogger(__name__)

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

# Standard sunrise/sunset elevation (refraction plus the sun's radius), and the
# civil twilight limit.
HORIZON_DEG = -0.833
CIVIL_DEG = -6.0


def twilight_phase(elevation_deg: float) -> str:
    """Classify a solar elevation as day, civil_twilight or night.

    "night" here means darker than civil twilight: nautical and astronomical
    twilight are both dark enough to need the porch light.
    """
    if elevation_deg >= HORIZON_DEG:
        return "day"
    if elevation_deg >= CIVIL_DEG:
        return "civil_twilight"
    return "night"


def sun_context(lat: float, lon: float, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    """Sun position and today's civil twilight times at home. Pure local computation."""
    observer = Observer(latitude=lat, longitude=lon)
    local_now = now.astimezone(tz)
    elev = elevation(observer, local_now)
    ctx: dict[str, Any] = {
        "local_time": local_now.isoformat(timespec="minutes"),
        "sun_elevation_deg": round(elev, 2),
        "sun_azimuth_deg": round(azimuth(observer, local_now), 1),
        "phase": twilight_phase(elev),
        "is_evening": local_now.hour >= 12,
    }
    try:
        times = sun(observer, date=local_now.date(), tzinfo=tz)
        for key in ("dawn", "sunrise", "sunset", "dusk"):
            ctx[f"civil_{key}" if key in ("dawn", "dusk") else key] = times[key].isoformat(
                timespec="minutes"
            )
    except ValueError:
        # Polar day or night: the sun never crosses the relevant elevation today.
        pass
    return ctx


class WeatherError(RuntimeError):
    """Current conditions could not be obtained."""


@dataclass
class _CacheEntry:
    fetched_at: float
    data: dict[str, Any]


class WeatherClient:
    """OpenWeatherMap current conditions, cached for ``ttl_s`` seconds.

    The cache lives in memory and, if ``cache_file`` is given, on disk too, so
    back-to-back ``simulate`` runs share it as well as arrivals within one
    long-running monitor.
    """

    def __init__(
        self,
        api_key: str | None,
        lat: float,
        lon: float,
        ttl_s: float = 600.0,
        cache_file: Path | None = None,
        timeout_s: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.time,
    ) -> None:
        self._api_key = api_key
        self._lat, self._lon = lat, lon
        self._ttl = ttl_s
        self._cache_file = cache_file
        self._timeout = timeout_s
        self._transport = transport
        self._clock = clock
        self._entry: _CacheEntry | None = None

    def _fresh(self, entry: _CacheEntry | None) -> bool:
        return entry is not None and self._clock() - entry.fetched_at < self._ttl

    def _load_disk(self) -> _CacheEntry | None:
        if self._cache_file is None or not self._cache_file.is_file():
            return None
        try:
            doc = json.loads(self._cache_file.read_text())
            if doc.get("lat") != self._lat or doc.get("lon") != self._lon:
                return None
            return _CacheEntry(float(doc["fetched_at"]), dict(doc["data"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_disk(self, entry: _CacheEntry) -> None:
        if self._cache_file is None:
            return
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                json.dumps(
                    {"lat": self._lat, "lon": self._lon,
                     "fetched_at": entry.fetched_at, "data": entry.data}
                )
            )
        except OSError as exc:
            log.warning("Could not write weather cache %s: %s", self._cache_file, exc)

    async def current(self) -> dict[str, Any]:
        """Current conditions. Raises WeatherError if they cannot be obtained."""
        if not self._fresh(self._entry):
            disk = self._load_disk()
            if self._fresh(disk):
                self._entry = disk
        if self._fresh(self._entry):
            assert self._entry is not None
            age = self._clock() - self._entry.fetched_at
            return {**self._entry.data, "cached": True, "age_s": round(age)}

        if not self._api_key:
            raise WeatherError("OWM_API_KEY is not set.")
        params = {"lat": self._lat, "lon": self._lon, "appid": self._api_key, "units": "metric"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                response = await http.get(OWM_URL, params=params)
            response.raise_for_status()
            data = summarise_owm(response.json())
        except httpx.HTTPStatusError as exc:
            # Never include the URL: it carries the API key.
            raise WeatherError(f"OpenWeatherMap returned HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            raise WeatherError(f"OpenWeatherMap request failed: {type(exc).__name__}") from None
        except (ValueError, KeyError, TypeError) as exc:
            raise WeatherError(f"Unexpected OpenWeatherMap response: {exc}") from None

        self._entry = _CacheEntry(self._clock(), data)
        self._save_disk(self._entry)
        return {**data, "cached": False, "age_s": 0}


# OpenWeatherMap condition id groups: https://openweathermap.org/weather-conditions
def summarise_owm(doc: dict[str, Any]) -> dict[str, Any]:
    """Reduce an OWM /weather response to the fields that matter for lighting."""
    weather = (doc.get("weather") or [{}])[0]
    code = int(weather.get("id", 800))
    group = code // 100
    return {
        "condition": weather.get("main", "Unknown"),
        "description": weather.get("description", ""),
        "condition_id": code,
        "temp_c": doc.get("main", {}).get("temp"),
        "cloud_cover_pct": doc.get("clouds", {}).get("all"),
        "visibility_m": doc.get("visibility"),
        "rain_1h_mm": (doc.get("rain") or {}).get("1h", 0),
        "snow_1h_mm": (doc.get("snow") or {}).get("1h", 0),
        "is_rain": group in (2, 3, 5),
        "is_snow": group == 6,
        # 7xx is atmosphere: mist, fog, haze, smoke, dust...
        "is_fog": group == 7,
    }
