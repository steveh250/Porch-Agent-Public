"""Presence state machine with hysteresis. Pure logic: no I/O, no clock.

    UNKNOWN --first fix--> AWAY (beyond outer) or NEAR (within outer, no fire)
    AWAY --< outer--> APPROACHING   fires the agent
    AWAY --< inner--> NEAR          fires the agent (sparse fixes can skip a band)
    APPROACHING --< inner--> NEAR   no fire
    APPROACHING/NEAR --> AWAY       only once distance exceeds OUTER again (re-arm)

Moving back out between inner and outer while NEAR changes nothing: the arrival
has already been handled, and only leaving past the outer radius re-arms it.

The first fix after startup never fires. If the monitor restarts while you are
at home, that is not an arrival.

A stale fix (see ``update(..., may_fire=False)``) moves the state like any other
but never fires. That is what makes a backlog of queued fixes harmless: the
machine follows you from away to home through the backlog, so the fresh fix
that follows finds you already NEAR and does not fire either.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Presence(str, Enum):
    UNKNOWN = "unknown"
    AWAY = "away"
    APPROACHING = "approaching"
    NEAR = "near"


@dataclass(frozen=True)
class Transition:
    previous: Presence
    current: Presence
    distance_m: float
    fire: bool
    # True when this would have been an arrival but the fix was too old to act on.
    suppressed: bool = False


class PresenceTracker:
    def __init__(self, outer_radius_m: float, inner_radius_m: float) -> None:
        if not 0 < inner_radius_m < outer_radius_m:
            raise ValueError("inner radius must be positive and smaller than outer radius")
        self.outer = outer_radius_m
        self.inner = inner_radius_m
        self.state = Presence.UNKNOWN

    def update(self, distance_m: float, may_fire: bool = True) -> Transition | None:
        """Feed one accurate fix's distance. Returns a Transition if the state changed.

        ``may_fire=False`` (a stale fix) still moves the state, but an arrival
        it would have caused is reported as suppressed instead of fired.
        """
        previous = self.state
        new = self._next(previous, distance_m)
        if new is previous:
            return None
        self.state = new
        arrival = previous is Presence.AWAY and new in (Presence.APPROACHING, Presence.NEAR)
        return Transition(previous, new, distance_m, arrival and may_fire,
                          suppressed=arrival and not may_fire)

    def _next(self, state: Presence, d: float) -> Presence:
        if state is Presence.UNKNOWN:
            return Presence.AWAY if d > self.outer else Presence.NEAR
        if d > self.outer:
            return Presence.AWAY
        if d < self.inner:
            return Presence.NEAR
        # In the band [inner, outer]: approaching if coming from away, otherwise
        # stay where we are (hysteresis).
        if state is Presence.AWAY and d < self.outer:
            return Presence.APPROACHING
        return state
