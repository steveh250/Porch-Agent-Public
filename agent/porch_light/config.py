"""Configuration: ``config.yaml`` for settings, ``.env`` for secrets.

Relative paths in the YAML are resolved against the directory the YAML lives
in, so the agent behaves the same whichever directory it is started from.
Secrets already in the process environment win over ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_MODEL = "google/gemini-2.5-flash"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


class _DuplicateKey(ValueError):
    pass


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects a key repeated in the same mapping.

    Plain YAML silently keeps the last of two identical keys, so a pasted second
    ``username:`` or ``mqtt:`` block quietly overrides the first. Refuse instead.
    """


def _construct_unique_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen: dict[Any, int] = {}
    for key_node, _value in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key == "<<":  # YAML merge key; may legitimately repeat
            continue
        line = key_node.start_mark.line + 1
        if key in seen:
            raise _DuplicateKey(
                f"line {line}: '{key}' is defined twice (first on line {seen[key]}). "
                f"YAML would silently use the last one; delete one of them."
            )
        seen[key] = line
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True)
class HomeConfig:
    lat: float
    lon: float
    timezone: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class PresenceConfig:
    outer_radius_m: float = 400.0
    inner_radius_m: float = 200.0
    max_accuracy_m: float = 100.0
    # A fix older than this can move the state machine but never fire an arrival.
    # 0 disables the check.
    max_fix_age_s: float = 120.0


@dataclass(frozen=True)
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    topic: str = "owntracks/+/+"
    client_id: str = "porch-light"
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class LlmConfig:
    model: str = DEFAULT_MODEL
    base_url: str = OPENROUTER_BASE_URL
    api_key: str | None = None
    timeout_s: float = 30.0


@dataclass(frozen=True)
class McpConfig:
    url: str = "http://127.0.0.1:8000/mcp"
    # Budget for the deterministic path (fallback and verification) per call.
    timeout_s: float = 10.0


@dataclass(frozen=True)
class BulbConfig:
    # Used only while the server cannot report the bulb's real range (it reports
    # null until the bulb has been reached). 2200-6500K is typical for WiZ.
    kelvin_min: int = 2200
    kelvin_max: int = 6500
    min_brightness_pct: int = 10
    max_brightness_pct: int = 100


@dataclass(frozen=True)
class FallbackConfig:
    brightness_pct: int = 80
    color_temp_kelvin: int = 2700
    # Civil dusk: the sun is 6 degrees below the horizon.
    sun_elevation_deg: float = -6.0


@dataclass(frozen=True)
class RestingConfig:
    """The light's state when nobody is arriving (see resting.py)."""

    enabled: bool = True
    brightness_pct: int = 20
    color_temp_kelvin: int = 2700
    # Below this sun elevation it is "night" and the light rests on. -6 is civil
    # dusk/dawn, the same line the fallback uses.
    sun_elevation_deg: float = -6.0
    # After an arrival changes the light, go back to resting this much later.
    revert_after_min: float = 10.0
    check_interval_s: float = 60.0


@dataclass(frozen=True)
class WeatherConfig:
    api_key: str | None = None
    cache_ttl_s: float = 600.0
    cache_file: Path | None = None
    timeout_s: float = 8.0


@dataclass(frozen=True)
class Config:
    home: HomeConfig
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    bulb: BulbConfig = field(default_factory=BulbConfig)
    fallback: FallbackConfig = field(default_factory=FallbackConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    resting: RestingConfig = field(default_factory=RestingConfig)
    policy_file: Path = Path("policy.md")
    event_log: Path = Path("logs/arrivals.jsonl")


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' in the config must be a mapping.")
    return value


def _number(section: dict[str, Any], key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} must be a number, got {value!r}.")
    return float(value)


def _secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_config(
    path: str | os.PathLike[str] = DEFAULT_CONFIG_FILE,
    env_file: str | os.PathLike[str] | None = None,
) -> Config:
    """Load ``path`` and the secrets from ``env_file`` (default: ``.env`` beside it)."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(
            f"{config_path} not found. Copy config.example.yaml to config.yaml "
            f"and set your home coordinates."
        )
    base = config_path.resolve().parent
    env_path = Path(env_file) if env_file is not None else base / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)

    try:
        raw = yaml.load(config_path.read_text(), Loader=_StrictLoader) or {}
    except _DuplicateKey as exc:
        raise ConfigError(f"{config_path}, {exc}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a mapping at the top level.")

    home_raw = _section(raw, "home")
    if "lat" not in home_raw or "lon" not in home_raw:
        raise ConfigError("home.lat and home.lon are required.")
    home = HomeConfig(
        lat=_number(home_raw, "lat", 0, "home"),
        lon=_number(home_raw, "lon", 0, "home"),
        timezone=str(home_raw.get("timezone", "UTC")),
    )
    if not -90 <= home.lat <= 90 or not -180 <= home.lon <= 180:
        raise ConfigError("home.lat/home.lon are out of range.")
    try:
        home.tz
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"home.timezone {home.timezone!r} is not a known IANA zone.") from None

    p = _section(raw, "presence")
    presence = PresenceConfig(
        outer_radius_m=_number(p, "outer_radius_m", 400, "presence"),
        inner_radius_m=_number(p, "inner_radius_m", 200, "presence"),
        max_accuracy_m=_number(p, "max_accuracy_m", 100, "presence"),
        max_fix_age_s=_number(p, "max_fix_age_s", 120, "presence"),
    )
    if presence.max_fix_age_s < 0:
        raise ConfigError("presence.max_fix_age_s must be 0 (off) or a positive number of seconds.")
    if not 0 < presence.inner_radius_m < presence.outer_radius_m:
        raise ConfigError(
            "presence.inner_radius_m must be positive and smaller than outer_radius_m."
        )

    m = _section(raw, "mqtt")
    mqtt = MqttConfig(
        host=str(m.get("host", "localhost")),
        port=int(_number(m, "port", 1883, "mqtt")),
        topic=str(m.get("topic", "owntracks/+/+")),
        client_id=str(m.get("client_id", "porch-light")),
        username=m.get("username") or None,
        password=_secret("MQTT_PASSWORD"),
    )

    l = _section(raw, "llm")
    llm = LlmConfig(
        model=str(l.get("model", DEFAULT_MODEL)),
        base_url=str(l.get("base_url", OPENROUTER_BASE_URL)),
        api_key=_secret("OPENROUTER_API_KEY"),
        timeout_s=_number(l, "timeout_s", 30, "llm"),
    )

    c = _section(raw, "mcp")
    mcp = McpConfig(
        url=str(c.get("url", McpConfig.url)),
        timeout_s=_number(c, "timeout_s", 10, "mcp"),
    )

    b = _section(raw, "bulb")
    bulb = BulbConfig(
        kelvin_min=int(_number(b, "kelvin_min", 2200, "bulb")),
        kelvin_max=int(_number(b, "kelvin_max", 6500, "bulb")),
        min_brightness_pct=int(_number(b, "min_brightness_pct", 10, "bulb")),
        max_brightness_pct=int(_number(b, "max_brightness_pct", 100, "bulb")),
    )
    if bulb.kelvin_min > bulb.kelvin_max:
        raise ConfigError("bulb.kelvin_min must not exceed bulb.kelvin_max.")
    if not 1 <= bulb.min_brightness_pct <= bulb.max_brightness_pct <= 100:
        raise ConfigError("bulb brightness limits must satisfy 1 <= min <= max <= 100.")

    f = _section(raw, "fallback")
    fallback = FallbackConfig(
        brightness_pct=int(_number(f, "brightness_pct", 80, "fallback")),
        color_temp_kelvin=int(_number(f, "color_temp_kelvin", 2700, "fallback")),
        sun_elevation_deg=_number(f, "sun_elevation_deg", -6.0, "fallback"),
    )

    w = _section(raw, "weather")
    cache_file = w.get("cache_file", "state/weather_cache.json")
    weather = WeatherConfig(
        api_key=_secret("OWM_API_KEY"),
        cache_ttl_s=_number(w, "cache_ttl_s", 600, "weather"),
        cache_file=(base / cache_file) if cache_file else None,
        timeout_s=_number(w, "timeout_s", 8, "weather"),
    )

    rs = _section(raw, "resting")
    enabled = rs.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"resting.enabled must be true or false, got {enabled!r}.")
    resting = RestingConfig(
        enabled=enabled,
        brightness_pct=int(_number(rs, "brightness_pct", 20, "resting")),
        color_temp_kelvin=int(_number(rs, "color_temp_kelvin", 2700, "resting")),
        sun_elevation_deg=_number(rs, "sun_elevation_deg", -6.0, "resting"),
        revert_after_min=_number(rs, "revert_after_min", 10, "resting"),
        check_interval_s=_number(rs, "check_interval_s", 60, "resting"),
    )
    if resting.revert_after_min < 0:
        raise ConfigError("resting.revert_after_min must be 0 or more minutes.")
    if resting.check_interval_s <= 0:
        raise ConfigError("resting.check_interval_s must be a positive number of seconds.")

    paths = _section(raw, "paths")
    return Config(
        home=home,
        presence=presence,
        mqtt=mqtt,
        llm=llm,
        mcp=mcp,
        bulb=bulb,
        fallback=fallback,
        weather=weather,
        resting=resting,
        policy_file=base / str(paths.get("policy", "policy.md")),
        event_log=base / str(paths.get("event_log", "logs/arrivals.jsonl")),
    )
