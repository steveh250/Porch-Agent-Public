"""The resting light: what the porch light does when nobody is arriving. No LLM.

    civil dusk ─────────── night: rest at 20 % / 2700 K ─────────── civil dawn
    daylight: off

An arrival hands the light to the agent. Ten minutes later (``revert_after_min``)
the light goes back to its resting state for the time of day.

It only acts when something changes: at dusk, at dawn, after an arrival's hold
ends, and once at startup. So if you set the light by hand, it is left alone
until the next of those. A change that fails (bulb unreachable) is retried on the
next check.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .arrival import ArrivalDeps, _apply, _with_light
from .config import Config
from .guardrails import Decision, resolve_kelvin_range, verify
from .lightctl import LightError
from .situation import sun_context

log = logging.getLogger(__name__)


class RestingLight:
    def __init__(
        self,
        config: Config,
        deps: ArrivalDeps,
        lock: asyncio.Lock | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.deps = deps
        # Shared with arrival handling, so a revert never lands mid-arrival.
        self.lock = lock or asyncio.Lock()
        self.clock = clock or deps.clock
        self.applied: Decision | None = None  # None: (re)apply on the next tick
        self.hold_until: datetime | None = None

    def target(self, now: datetime) -> Decision:
        """The resting state for this moment: low at night, off in daylight."""
        r = self.config.resting
        sun = sun_context(self.config.home.lat, self.config.home.lon, self.config.home.tz, now)
        if float(sun["sun_elevation_deg"]) < r.sun_elevation_deg:
            return Decision("on", r.brightness_pct, r.color_temp_kelvin)
        return Decision("off")

    def note_arrival(self, record: dict[str, Any], now: datetime | None = None) -> None:
        """Hold the light for the agent, then revert, if the arrival changed it."""
        decision = (record or {}).get("decision") or {}
        if decision.get("action") not in ("on", "off"):
            return  # the arrival left the light alone; nothing to revert
        now = now or self.clock()
        self.hold_until = now + timedelta(minutes=self.config.resting.revert_after_min)
        self.applied = None
        log.info("Arrival set the light; back to resting at %s",
                 self.hold_until.astimezone(self.config.home.tz).strftime("%H:%M"))

    async def tick(self, now: datetime | None = None) -> Decision | None:
        """Apply the resting state if it is due. Returns what was applied, if anything."""
        now = now or self.clock()
        if self.hold_until is not None:
            if now < self.hold_until:
                return None
            self.hold_until = None
            self.applied = None
        target = self.target(now)
        if target == self.applied:
            return None
        async with self.lock:
            try:
                state = await _with_light(self.config, self.deps, lambda light: light.get_state())
                decision = target.clamped(self.config.bulb, resolve_kelvin_range(state, self.config.bulb))
                await _with_light(self.config, self.deps, lambda light: _apply(light, decision))
                after = await _with_light(self.config, self.deps, lambda light: light.get_state())
            except LightError as exc:
                log.warning("Resting light not set (%s); will retry", exc)
                return None
        check = verify(decision, after)
        if not check.ok:
            log.warning("Resting light set but not confirmed (%s); will retry", check.detail)
            return None
        self.applied = target
        log.info("Resting light: %s", _describe(decision))
        return decision

    async def run_forever(self) -> None:
        interval = self.config.resting.check_interval_s
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("Resting light check failed; continuing")
            await asyncio.sleep(interval)


def _describe(decision: Decision) -> str:
    if decision.action == "on":
        return f"on at {decision.brightness_pct} % / {decision.color_temp_kelvin} K"
    return decision.action
