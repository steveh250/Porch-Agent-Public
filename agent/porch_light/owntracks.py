"""Parsing OwnTracks MQTT payloads.

OwnTracks publishes several message types on the same topic (location,
transition, waypoint, lwt, ...). Only ``_type == "location"`` carries a fix.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fix:
    lat: float
    lon: float
    acc: float
    tst: datetime | None = None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_location(payload: bytes | str) -> Fix | None:
    """Return the fix in an OwnTracks payload, or None if it is not a usable location.

    Anything that is not valid JSON, not a location message, or lacks numeric
    coordinates is ignored. A location without ``acc`` is also rejected: with no
    accuracy there is no way to tell a good fix from a cell-tower guess, and a
    bad one could fire the agent.
    """
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        log.debug("Ignoring non-JSON payload")
        return None
    if not isinstance(doc, dict) or doc.get("_type") != "location":
        return None

    lat, lon, acc = _number(doc.get("lat")), _number(doc.get("lon")), _number(doc.get("acc"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        log.debug("Ignoring location without valid coordinates")
        return None
    if acc is None or acc < 0:
        log.debug("Ignoring location without an accuracy value")
        return None

    tst = _number(doc.get("tst"))
    when = datetime.fromtimestamp(tst, timezone.utc) if tst is not None else None
    return Fix(lat=lat, lon=lon, acc=acc, tst=when)


def is_accurate(fix: Fix, max_accuracy_m: float) -> bool:
    """True if the fix's accuracy radius is within ``max_accuracy_m``."""
    return fix.acc <= max_accuracy_m
