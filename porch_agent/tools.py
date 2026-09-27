"""The eight MCP tools.

Everything an agent sees is defined here: percentages rather than the device's
0-255 brightness scale, scene names rather than numeric ids, and validation that
happens before any request reaches the bulb.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pywizlight import PilotBuilder

from .bulb import BulbController, BulbError, Snapshot, ValidationError, pct_to_raw

RGB_MIN, RGB_MAX = 0, 255
PCT_MIN, PCT_MAX = 0, 100

TOOL_NAMES = (
    "get_light_state",
    "turn_on",
    "turn_off",
    "set_brightness",
    "set_color",
    "set_color_temp",
    "set_scene",
    "list_scenes",
)

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, idempotent_hint=True)


@dataclass
class AppContext:
    """What the server's lifespan hands to every tool call."""

    bulb: BulbController


def _bulb(ctx: Context) -> BulbController:
    app: AppContext = ctx.request_context.lifespan_context
    return app.bulb


def _fail(exc: Exception) -> ToolError:
    """Turn a domain error into a ToolError, keeping its code visible to the agent."""
    code = getattr(exc, "code", "error")
    return ToolError(f"[{code}] {exc}")


def _snapshot_dict(snap: Snapshot) -> dict[str, Any]:
    state = snap.state
    return {
        "reachable": snap.reachable,
        "ever_confirmed": snap.last_confirmed is not None,
        "last_confirmed": (
            snap.last_confirmed.isoformat() if snap.last_confirmed else None
        ),
        "state": (
            None
            if state is None
            else {
                "on": state.on,
                "brightness_pct": state.brightness_pct,
                "rgb": list(state.rgb) if state.rgb else None,
                "color_temp_kelvin": state.color_temp_kelvin,
                "scene": state.scene,
            }
        ),
    }


def _kelvin_range_dict(bulb: BulbController) -> dict[str, int] | None:
    """The bulb's supported colour temperature range, or None while unknown."""
    capabilities = bulb.capabilities
    kelvin_range = capabilities.kelvin_range if capabilities is not None else None
    if kelvin_range is None:
        return None
    return {"min": kelvin_range.min, "max": kelvin_range.max}


def _check_pct(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"brightness_pct must be a whole number, got {value!r}.")
    if not PCT_MIN <= value <= PCT_MAX:
        raise ValidationError(
            f"brightness_pct must be between {PCT_MIN} and {PCT_MAX}, got {value}."
        )
    return value


def _check_rgb(red: int, green: int, blue: int) -> tuple[int, int, int]:
    for name, value in (("red", red), ("green", green), ("blue", blue)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"{name} must be a whole number, got {value!r}.")
        if not RGB_MIN <= value <= RGB_MAX:
            raise ValidationError(
                f"{name} must be between {RGB_MIN} and {RGB_MAX}, got {value}."
            )
    return red, green, blue


def _check_kelvin(bulb: BulbController, kelvin: int) -> int:
    # Validated against the range the bulb itself reports, not a hard-coded one.
    kelvin_range = bulb.require_capabilities().kelvin_range
    if kelvin_range is None:
        raise ValidationError(
            f"The device at {bulb.address} does not report a colour temperature range."
        )
    if not kelvin_range.min <= kelvin <= kelvin_range.max:
        raise ValidationError(
            f"color_temp_kelvin must be between {kelvin_range.min} and "
            f"{kelvin_range.max} for this bulb, got {kelvin}."
        )
    return kelvin


def register_tools(mcp: MCPServer) -> None:
    """Register all eight tools on the server."""

    @mcp.tool(
        annotations=READ_ONLY,
        description=(
            "Report the light's last known state, whether the bulb is currently "
            "reachable, when that was last confirmed, and the colour temperature "
            "range it supports (null until the bulb has been reached). Returns "
            "immediately and never waits on the bulb, so it is safe to call when "
            "the bulb is off."
        ),
    )
    async def get_light_state(ctx: Context) -> dict[str, Any]:
        # Served entirely from the poll cache: no device call.
        bulb = _bulb(ctx)
        doc = _snapshot_dict(bulb.snapshot())
        doc["kelvin_range"] = _kelvin_range_dict(bulb)
        return doc

    @mcp.tool(
        annotations=MUTATING,
        description=(
            "Switch the light on, optionally applying brightness, colour, or "
            "colour temperature in the same call."
        ),
    )
    async def turn_on(
        ctx: Context,
        brightness_pct: int | None = None,
        red: int | None = None,
        green: int | None = None,
        blue: int | None = None,
        color_temp_kelvin: int | None = None,
    ) -> dict[str, Any]:
        bulb = _bulb(ctx)
        colour_parts = [red, green, blue]
        try:
            kwargs: dict[str, Any] = {}
            if brightness_pct is not None:
                # Range before feature check, as in set_brightness.
                kwargs["brightness"] = pct_to_raw(_check_pct(brightness_pct))
                bulb.require_feature("brightness", "brightness")
            if any(part is not None for part in colour_parts):
                if any(part is None for part in colour_parts):
                    raise ValidationError(
                        "red, green and blue must all be given together."
                    )
                kwargs["rgb"] = _check_rgb(red, green, blue)  # type: ignore[arg-type]
                bulb.require_feature("color", "colour")
            if color_temp_kelvin is not None:
                if "rgb" in kwargs:
                    raise ValidationError(
                        "Give either a colour or a colour temperature, not both."
                    )
                bulb.require_feature("color_tmp", "colour temperature")
                kwargs["colortemp"] = _check_kelvin(bulb, color_temp_kelvin)
            snap = await bulb.turn_on(PilotBuilder(**kwargs) if kwargs else None)
        except (ValidationError, BulbError) as exc:
            raise _fail(exc) from None
        return _snapshot_dict(snap)

    @mcp.tool(annotations=MUTATING, description="Switch the light off.")
    async def turn_off(ctx: Context) -> dict[str, Any]:
        try:
            return _snapshot_dict(await _bulb(ctx).turn_off())
        except BulbError as exc:
            raise _fail(exc) from None

    @mcp.tool(
        annotations=MUTATING,
        description=(
            "Set brightness as a percentage from 0 to 100. The device stores a "
            "coarser scale, so a value read back may differ slightly."
        ),
    )
    async def set_brightness(ctx: Context, brightness_pct: int) -> dict[str, Any]:
        bulb = _bulb(ctx)
        try:
            # Range is validated before the feature check, so bad input is always a
            # validation error -- even when the bulb has never been reachable and
            # its capabilities are unknown.
            raw = pct_to_raw(_check_pct(brightness_pct))
            bulb.require_feature("brightness", "brightness")
            # PilotBuilder's `brightness` is the device's 0-255 scale.
            snap = await bulb.apply(PilotBuilder(brightness=raw), "set brightness")
        except (ValidationError, BulbError) as exc:
            raise _fail(exc) from None
        return _snapshot_dict(snap)

    @mcp.tool(
        annotations=MUTATING,
        description="Set the light's colour from red, green and blue components (0-255).",
    )
    async def set_color(ctx: Context, red: int, green: int, blue: int) -> dict[str, Any]:
        bulb = _bulb(ctx)
        try:
            # Range first, for the same reason as set_brightness.
            rgb = _check_rgb(red, green, blue)
            bulb.require_feature("color", "colour")
            snap = await bulb.apply(PilotBuilder(rgb=rgb), "set colour")
        except (ValidationError, BulbError) as exc:
            raise _fail(exc) from None
        return _snapshot_dict(snap)

    @mcp.tool(
        annotations=MUTATING,
        description=(
            "Set the white colour temperature in Kelvin. Valid range is whatever "
            "this bulb reports; call get_light_state first if unsure."
        ),
    )
    async def set_color_temp(ctx: Context, color_temp_kelvin: int) -> dict[str, Any]:
        bulb = _bulb(ctx)
        try:
            bulb.require_feature("color_tmp", "colour temperature")
            kelvin = _check_kelvin(bulb, color_temp_kelvin)
            # PilotBuilder takes `colortemp`, not `kelvin` as its README shows.
            snap = await bulb.apply(
                PilotBuilder(colortemp=kelvin), "set colour temperature"
            )
        except (ValidationError, BulbError) as exc:
            raise _fail(exc) from None
        return _snapshot_dict(snap)

    @mcp.tool(
        annotations=MUTATING,
        description=(
            "Select a lighting scene by name, such as 'Cozy' or 'Candlelight'. "
            "Case does not matter. Use list_scenes to see what this bulb supports."
        ),
    )
    async def set_scene(ctx: Context, name: str) -> dict[str, Any]:
        bulb = _bulb(ctx)
        try:
            # Name is validated first, so a typo is a validation error even with an
            # unreachable bulb; the feature check needs capabilities and follows.
            scene_id = bulb.scene_id_for(name)
            bulb.require_feature("effect", "scenes")
            snap = await bulb.apply(PilotBuilder(scene=scene_id), "set scene")
        except (ValidationError, BulbError) as exc:
            raise _fail(exc) from None
        return _snapshot_dict(snap)

    @mcp.tool(
        annotations=READ_ONLY,
        description="List the scene names this bulb supports.",
    )
    async def list_scenes(ctx: Context) -> dict[str, Any]:
        try:
            # Derived from cached capabilities: no device call.
            return {"scenes": _bulb(ctx).supported_scene_names()}
        except BulbError as exc:
            raise _fail(exc) from None
