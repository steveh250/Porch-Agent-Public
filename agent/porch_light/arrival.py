"""One arrival, end to end. This is the deterministic wrapper around the agent.

    sun (local)  ->  weather  ->  agent (30 s hard timeout)  ->  verify  ->  log
                        |               |
                        +---- fails ----+--> fallback: 80 % / 2700 K after
                                             civil dusk, nothing in daylight

Whatever happens, the arrival ends with exactly one JSONL record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .agent import AgentOutcome, run_agent
from .config import Config
from .guardrails import (
    Decision,
    fallback_decision,
    resolve_kelvin_range,
    verify,
)
from .lightctl import LightClient, LightError, McpLightClient
from .situation import WeatherClient, WeatherError, sun_context

log = logging.getLogger(__name__)


class WeatherSource(Protocol):
    async def current(self) -> dict[str, Any]: ...


AgentRunner = Callable[..., Awaitable[AgentOutcome]]
LightFactory = Callable[[], AbstractAsyncContextManager[LightClient]]


@dataclass
class ArrivalDeps:
    light_factory: LightFactory
    weather: WeatherSource
    agent_runner: AgentRunner = run_agent
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def default_deps(config: Config) -> ArrivalDeps:
    return ArrivalDeps(
        light_factory=lambda: McpLightClient(config.mcp.url, config.mcp.timeout_s),
        weather=WeatherClient(
            api_key=config.weather.api_key,
            lat=config.home.lat,
            lon=config.home.lon,
            ttl_s=config.weather.cache_ttl_s,
            cache_file=config.weather.cache_file,
            timeout_s=config.weather.timeout_s,
        ),
    )


async def _with_light(
    config: Config, deps: ArrivalDeps, op: Callable[[LightClient], Awaitable[Any]]
) -> Any:
    """Open a session, run ``op``, close -- all within the MCP timeout."""
    try:
        async with asyncio.timeout(config.mcp.timeout_s):
            async with deps.light_factory() as light:
                return await op(light)
    except LightError:
        raise
    except TimeoutError:
        raise LightError(f"no answer from the MCP server within {config.mcp.timeout_s:g}s") from None
    except Exception as exc:  # noqa: BLE001 - transport errors arrive in many shapes
        cause = _root_cause(exc)
        if isinstance(cause, LightError):
            raise cause from exc
        raise LightError(
            f"MCP server at {config.mcp.url}: {type(cause).__name__}: {cause}"
        ) from exc


def _root_cause(exc: BaseException) -> BaseException:
    """The first leaf of an ExceptionGroup (the MCP client uses anyio task groups)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def _apply(light: LightClient, decision: Decision) -> None:
    if decision.action == "on":
        await light.turn_on(decision.brightness_pct, decision.color_temp_kelvin)
    elif decision.action == "off":
        await light.turn_off()


async def handle_arrival(
    config: Config,
    deps: ArrivalDeps,
    distance_m: float,
    *,
    force_fallback: bool = False,
) -> dict[str, Any]:
    """Handle one arrival and append its record to the event log. Never raises."""
    started = time.monotonic()
    now = deps.clock()
    errors: list[str] = []
    sun = sun_context(config.home.lat, config.home.lon, config.home.tz, now)
    record: dict[str, Any] = {
        "timestamp": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "distance_m": round(distance_m, 1),
        "situation": {"sun": sun, "weather": None},
        "decision": None,
        "reason": None,
        "decided_by": None,
        "fallback_used": False,
        "agent": None,
        "light_before": None,
        "verification": None,
        "errors": errors,
    }

    # 1. Current bulb state: gives the Kelvin range for clamping. Not fatal.
    before: dict[str, Any] | None = None
    try:
        before = await _with_light(config, deps, lambda light: light.get_state())
        record["light_before"] = before.get("state")
    except LightError as exc:
        errors.append(f"read state before: {exc}")
    kelvin_range = resolve_kelvin_range(before, config.bulb)

    # 2. Weather. A failure goes straight to the fallback, without the LLM.
    weather: dict[str, Any] | None = None
    fallback_why: str | None = "forced by --force-fallback" if force_fallback else None
    if fallback_why is None:
        try:
            weather = await deps.weather.current()
            record["situation"]["weather"] = weather
        except WeatherError as exc:
            fallback_why = f"weather unavailable: {exc}"
        except Exception as exc:  # noqa: BLE001 - any weather failure means fallback
            fallback_why = f"weather unavailable: {type(exc).__name__}: {exc}"

    # 3. The agent, under a hard timeout.
    decision: Decision | None = None
    if fallback_why is None:
        try:
            async with asyncio.timeout(config.llm.timeout_s):
                outcome = await deps.agent_runner(
                    config,
                    distance_m=distance_m,
                    sun=sun,
                    weather=weather,
                    kelvin_range=kelvin_range,
                )
            decision = outcome.decision
            record["reason"] = outcome.reason
            record["decided_by"] = "agent"
            record["agent"] = {
                "model": config.llm.model,
                "duration_s": outcome.duration_s,
                "self_verified": outcome.self_verified,
                "tool_calls": [asdict(c) for c in outcome.tool_calls],
            }
        except TimeoutError:
            fallback_why = f"agent timed out after {config.llm.timeout_s:g}s"
        except Exception as exc:  # noqa: BLE001 - any agent failure means fallback
            fallback_why = f"agent failed: {type(exc).__name__}: {exc}"

    # 4. Fallback.
    if decision is None:
        record["fallback_used"] = True
        record["decided_by"] = "fallback"
        decision = fallback_decision(sun, config.fallback).clamped(config.bulb, kelvin_range)
        record["reason"] = (
            f"Fallback ({fallback_why}): "
            + ("after civil dusk, so light on at the safe default."
               if decision.action == "on" else "before civil dusk, so no action.")
        )
        errors.append(fallback_why or "unknown")
        if decision.action != "none":
            try:
                await _with_light(config, deps, lambda light: _apply(light, decision))
            except LightError as exc:
                errors.append(f"fallback apply: {exc}")
    record["decision"] = decision.to_dict()

    # 5. Independent verification, with one re-apply on mismatch. The agent is
    #    told to verify too, but the log records what the bulb actually reports.
    record["verification"] = await _verify(config, deps, decision, errors)

    record["duration_s"] = round(time.monotonic() - started, 2)
    append_event(config.event_log, record)
    log.info(
        "Arrival at %.0f m: %s by %s -- %s (verified: %s)",
        distance_m, decision.to_dict(), record["decided_by"], record["reason"],
        record["verification"]["ok"],
    )
    return record


async def _verify(
    config: Config, deps: ArrivalDeps, decision: Decision, errors: list[str]
) -> dict[str, Any]:
    if decision.action == "none":
        return {"ok": True, "detail": "no change requested", "attempts": 0, "reapplied": False}

    async def read() -> dict[str, Any] | None:
        try:
            return await _with_light(config, deps, lambda light: light.get_state())
        except LightError as exc:
            errors.append(f"verify read: {exc}")
            return None

    result = verify(decision, await read())
    reapplied = False
    if not result.ok:
        reapplied = True
        try:
            await _with_light(config, deps, lambda light: _apply(light, decision))
        except LightError as exc:
            errors.append(f"re-apply: {exc}")
        result = verify(decision, await read())
    return {**result.to_dict(), "attempts": 2 if reapplied else 1, "reapplied": reapplied}


def append_event(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line. A logging failure must never break an arrival."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        log.error("Could not write event log %s: %s", path, exc)
