"""Deterministic rules the LLM cannot override: clamping, verification, fallback."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .config import BulbConfig, FallbackConfig

Action = Literal["on", "off", "none"]

# The server stores brightness on a 0-255 scale, so a percentage read back can
# be off by one. WiZ bulbs also report colour temperature in coarse steps.
BRIGHTNESS_TOLERANCE_PCT = 2
KELVIN_TOLERANCE = 100


@dataclass(frozen=True)
class KelvinRange:
    min: int
    max: int


def resolve_kelvin_range(state_doc: dict[str, Any] | None, bulb: BulbConfig) -> KelvinRange:
    """The bulb's real range if the server reports it, else the configured one.

    When both are known the narrower intersection wins, so the config can
    restrict a bulb (to warm whites, say) but never widen it.
    """
    configured = KelvinRange(bulb.kelvin_min, bulb.kelvin_max)
    reported = (state_doc or {}).get("kelvin_range")
    if not isinstance(reported, dict):
        return configured
    try:
        lo, hi = int(reported["min"]), int(reported["max"])
    except (KeyError, TypeError, ValueError):
        return configured
    lo, hi = max(lo, configured.min), min(hi, configured.max)
    if lo > hi:
        # Config and bulb do not overlap; trust the bulb.
        return KelvinRange(int(reported["min"]), int(reported["max"]))
    return KelvinRange(lo, hi)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def clamp_brightness(value: Any, bulb: BulbConfig) -> int | None:
    """Clamp to [min_brightness_pct, max_brightness_pct]. None/garbage -> None."""
    number = _as_int(value)
    if number is None:
        return None
    return max(bulb.min_brightness_pct, min(bulb.max_brightness_pct, number))


def clamp_kelvin(value: Any, kelvin_range: KelvinRange) -> int | None:
    number = _as_int(value)
    if number is None:
        return None
    return max(kelvin_range.min, min(kelvin_range.max, number))


@dataclass(frozen=True)
class Decision:
    action: Action
    brightness_pct: int | None = None
    color_temp_kelvin: int | None = None

    def clamped(self, bulb: BulbConfig, kelvin_range: KelvinRange) -> "Decision":
        if self.action != "on":
            return Decision(self.action)
        return Decision(
            "on",
            clamp_brightness(self.brightness_pct, bulb),
            clamp_kelvin(self.color_temp_kelvin, kelvin_range),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_decision(doc: Any) -> Decision | None:
    """Build a Decision from the agent's final JSON, or None if it is unusable."""
    if not isinstance(doc, dict):
        return None
    action = str(doc.get("action", "")).strip().lower()
    if action not in ("on", "off", "none"):
        return None
    if action != "on":
        return Decision(action)  # type: ignore[arg-type]
    return Decision(
        "on", _as_int(doc.get("brightness_pct")), _as_int(doc.get("color_temp_kelvin"))
    )


def fallback_decision(sun: dict[str, Any], fallback: FallbackConfig) -> Decision:
    """Light on at the fallback setting after civil dusk; otherwise leave it alone."""
    if float(sun["sun_elevation_deg"]) < fallback.sun_elevation_deg:
        return Decision("on", fallback.brightness_pct, fallback.color_temp_kelvin)
    return Decision("none")


@dataclass(frozen=True)
class Verification:
    ok: bool
    detail: str
    observed: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify(decision: Decision, state_doc: dict[str, Any] | None) -> Verification:
    """Compare the bulb's reported state with what was decided."""
    if decision.action == "none":
        return Verification(True, "no change requested", (state_doc or {}).get("state"))
    if not state_doc or not state_doc.get("reachable"):
        return Verification(False, "bulb not reachable", None)
    state = state_doc.get("state")
    if not isinstance(state, dict):
        return Verification(False, "no state reported", None)

    if decision.action == "off":
        ok = state.get("on") is False
        return Verification(ok, "off as requested" if ok else "bulb is still on", state)

    problems: list[str] = []
    if state.get("on") is not True:
        problems.append("bulb is off")
    want_b, got_b = decision.brightness_pct, state.get("brightness_pct")
    if want_b is not None and (got_b is None or abs(got_b - want_b) > BRIGHTNESS_TOLERANCE_PCT):
        problems.append(f"brightness {got_b}% != {want_b}%")
    want_k, got_k = decision.color_temp_kelvin, state.get("color_temp_kelvin")
    if want_k is not None and (got_k is None or abs(got_k - want_k) > KELVIN_TOLERANCE):
        problems.append(f"colour temperature {got_k}K != {want_k}K")
    if problems:
        return Verification(False, "; ".join(problems), state)
    return Verification(True, "matches decision", state)
