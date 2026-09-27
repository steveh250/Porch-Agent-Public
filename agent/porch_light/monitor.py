"""The location monitor: OwnTracks fixes in, arrival events out. No LLM here.

``ArrivalDetector`` is the whole decision path from payload to "fire or not";
the MQTT subscriber and the simulator both just feed it payloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .geo import haversine_m
from .owntracks import Fix, is_accurate, parse_location
from .presence import PresenceTracker, Transition

log = logging.getLogger(__name__)

ArrivalHandler = Callable[[float], Awaitable[Any]]


@dataclass(frozen=True)
class Observation:
    fix: Fix
    distance_m: float
    transition: Transition | None
    age_s: float | None = None
    stale: bool = False


Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArrivalDetector:
    """Payload in, presence transition out.

    ``max_fix_age_s`` overrides the configured limit; pass 0 to turn the stale
    check off (the simulator does, because scripted fixes carry old timestamps).
    """

    def __init__(
        self, config: Config, clock: Clock = _utcnow, max_fix_age_s: float | None = None
    ) -> None:
        self.home_lat, self.home_lon = config.home.lat, config.home.lon
        self.max_accuracy_m = config.presence.max_accuracy_m
        self.max_fix_age_s = (
            config.presence.max_fix_age_s if max_fix_age_s is None else max_fix_age_s
        )
        self.clock = clock
        self.tracker = PresenceTracker(
            config.presence.outer_radius_m, config.presence.inner_radius_m
        )

    def process(self, payload: bytes | str) -> Observation | None:
        """Feed one payload. None if it was not an accurate location fix."""
        fix = parse_location(payload)
        if fix is None:
            return None
        if not is_accurate(fix, self.max_accuracy_m):
            log.debug("Discarding fix with accuracy %.0f m", fix.acc)
            return None
        distance = haversine_m(fix.lat, fix.lon, self.home_lat, self.home_lon)
        # Without a timestamp there is nothing to judge, so the fix counts as
        # current. A timestamp in the future (phone clock ahead) is current too.
        age = (self.clock() - fix.tst).total_seconds() if fix.tst is not None else None
        stale = bool(self.max_fix_age_s) and age is not None and age > self.max_fix_age_s
        transition = self.tracker.update(distance, may_fire=not stale)
        if transition is not None:
            if transition.fire:
                note = " (arrival: firing agent)"
            elif transition.suppressed:
                note = f" (arrival NOT fired: fix is {age:.0f} s old, limit {self.max_fix_age_s:g} s)"
            elif stale:
                note = f" (fix is {age:.0f} s old)"
            else:
                note = ""
            log.info(
                "Presence %s -> %s at %.0f m%s",
                transition.previous.value, transition.current.value, distance, note,
            )
        return Observation(fix, distance, transition, age, stale)


class ArrivalDispatcher:
    """Runs arrivals on the event loop, at most one at a time.

    The detector already fires at most once per arrival; this guards the case
    of leaving and coming back while a previous arrival is still being handled.
    """

    def __init__(self, handler: ArrivalHandler) -> None:
        self._handler = handler
        self._running: asyncio.Task | None = None

    def dispatch(self, distance_m: float) -> asyncio.Task | None:
        if self._running is not None and not self._running.done():
            log.warning("Arrival already being handled; ignoring a second one")
            return None
        self._running = asyncio.create_task(self._handler(distance_m), name="arrival")
        return self._running

    async def wait(self) -> None:
        if self._running is not None:
            await self._running


async def run_mqtt(config: Config, handler: ArrivalHandler, client_factory=None) -> None:
    """Subscribe to OwnTracks and run forever."""
    import paho.mqtt.client as mqtt

    loop = asyncio.get_running_loop()
    detector = ArrivalDetector(config)
    dispatcher = ArrivalDispatcher(handler)
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def on_connect(client, _userdata, _flags, reason_code, _properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("Connected to MQTT %s:%d; subscribing to %s",
                 config.mqtt.host, config.mqtt.port, config.mqtt.topic)
        # Subscribing here means a reconnect re-subscribes automatically.
        client.subscribe(config.mqtt.topic, qos=1)

    def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
        log.warning("MQTT disconnected (%s); paho will reconnect", reason_code)

    def on_message(_client, _userdata, message):
        # paho calls this on its own network thread; hand over to the loop.
        loop.call_soon_threadsafe(queue.put_nowait, bytes(message.payload))

    factory = client_factory or (
        lambda: mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=config.mqtt.client_id
        )
    )
    client = factory()
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(config.mqtt.host, config.mqtt.port, keepalive=60)
    client.loop_start()
    try:
        while True:
            payload = await queue.get()
            obs = detector.process(payload)
            if obs is not None and obs.transition is not None and obs.transition.fire:
                dispatcher.dispatch(obs.distance_m)
    finally:
        client.loop_stop()
        client.disconnect()
        await dispatcher.wait()


def load_fixes(path: Path) -> list[str]:
    """Read a simulation file: one OwnTracks JSON payload per line.

    Blank lines and lines starting with ``#`` are skipped. A line may also be a
    bare ``{"lat":..,"lon":..,"acc":..}`` object; ``_type: location`` is added.
    """
    payloads: list[str] = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            raise ValueError(f"{path}:{n}: not valid JSON") from None
        if isinstance(doc, dict):
            doc.setdefault("_type", "location")
        payloads.append(json.dumps(doc))
    return payloads


async def run_simulation(
    config: Config, handler: ArrivalHandler, payloads: Iterable[str], delay_s: float = 0.0
) -> list[Observation | None]:
    """Feed scripted payloads through the same detector the MQTT path uses.

    Unlike the live monitor, each arrival is awaited before the next fix, so
    the output reads in order.
    """
    # Scripted fixes carry fixed, long-past timestamps, so the stale check is off.
    detector = ArrivalDetector(config, max_fix_age_s=0)
    observations: list[Observation | None] = []
    for payload in payloads:
        obs = detector.process(payload)
        observations.append(obs)
        if obs is None:
            print("  fix ignored (not a location, or too inaccurate)")
        else:
            state = detector.tracker.state.value
            fire = obs.transition is not None and obs.transition.fire
            print(f"  {obs.distance_m:7.0f} m  acc {obs.fix.acc:4.0f} m  -> {state}"
                  + ("   ** ARRIVAL **" if fire else ""))
            if fire:
                await handler(obs.distance_m)
        if delay_s:
            await asyncio.sleep(delay_s)
    return observations
