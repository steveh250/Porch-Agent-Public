"""The single bulb connection: serialised access, presence polling, state cache.

Reads never touch the network. A background poll refreshes a cached snapshot, and
`get_light_state` serves that, so an unreachable bulb costs a caller nothing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from pywizlight import PilotBuilder, wizlight
from pywizlight.bulblibrary import BulbType
from pywizlight.exceptions import (
    WizLightConnectionError,
    WizLightError,
    WizLightMethodNotFound,
    WizLightNotKnownBulb,
    WizLightTimeOutError,
)
from pywizlight.scenes import SCENES_BY_CLASS, SCENE_NAME_TO_ID

log = logging.getLogger(__name__)

RAW_BRIGHTNESS_MAX = 255


class BulbError(RuntimeError):
    """A device operation failed. Carries a stable code for the tool layer."""

    code = "bulb_error"


class BulbTimeout(BulbError):
    code = "bulb_timeout"


class BulbUnreachable(BulbError):
    code = "bulb_unreachable"


class BulbRejected(BulbError):
    """The bulb answered, but refused the command."""

    code = "bulb_rejected"


class BulbUnknownModel(BulbError):
    code = "bulb_unknown_model"


class BulbUnsupportedOperation(BulbError):
    code = "bulb_unsupported_operation"


class BulbInternalError(BulbError):
    code = "internal_error"


class CapabilitiesUnknown(BulbError):
    """The bulb has never been reachable, so its capabilities are not known."""

    code = "capabilities_unknown"


class ValidationError(ValueError):
    """Input rejected before any request is sent to the bulb."""

    code = "validation_error"


def pct_to_raw(pct: int) -> int:
    """Convert an agent-facing 0-100 percentage to the device's 0-255 scale."""
    return round(pct * RAW_BRIGHTNESS_MAX / 100)


def raw_to_pct(raw: int) -> int:
    """Convert the device's 0-255 brightness to a percentage.

    Lossy in both directions: the device stores a coarser scale, so a value
    written and read back may differ slightly. That drift is expected, not a fault.
    """
    return round(raw * 100 / RAW_BRIGHTNESS_MAX)


@dataclass(frozen=True)
class LightState:
    """What the bulb reported the last time it was reached."""

    on: bool
    brightness_pct: int | None = None
    rgb: tuple[int, int, int] | None = None
    color_temp_kelvin: int | None = None
    scene: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """Cached state plus whether, and when, it was last confirmed."""

    reachable: bool
    # None means the bulb has never been reached since this process started.
    last_confirmed: datetime | None
    state: LightState | None


class BulbController:
    """Owns one `wizlight` instance, its cache, and its poll loop.

    Address and port are arguments rather than read from settings here, so the
    controller stays injectable.
    """

    def __init__(
        self,
        ip: str,
        port: int,
        request_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        self._ip = ip
        self._port = port
        self._timeout = request_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._light = wizlight(ip=ip, port=port)
        # One UDP socket shared by the poll task and every concurrent tool call.
        self._lock = asyncio.Lock()
        self._state: LightState | None = None
        self._last_confirmed: datetime | None = None
        self._reachable = False
        self._capabilities: BulbType | None = None

    @property
    def address(self) -> str:
        return f"{self._ip}:{self._port}"

    # --- cache ------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Current cached view. Never performs a device call."""
        return Snapshot(
            reachable=self._reachable,
            last_confirmed=self._last_confirmed,
            state=self._state,
        )

    @property
    def capabilities(self) -> BulbType | None:
        return self._capabilities

    def require_capabilities(self) -> BulbType:
        """Capabilities, or CapabilitiesUnknown if the bulb was never reached."""
        if self._capabilities is None:
            raise CapabilitiesUnknown(
                f"The bulb's capabilities are not yet known, because it has not been "
                f"reachable at {self.address} since this server started."
            )
        return self._capabilities

    def supported_scene_names(self) -> list[str]:
        """Scene names this bulb's class supports. No device call."""
        bulb_type = self.require_capabilities()
        return list(SCENES_BY_CLASS.get(bulb_type.bulb_type, []))

    def scene_id_for(self, name: str) -> int:
        """Resolve a scene name case-insensitively, or raise ValidationError.

        Checked against the full WiZ scene table first, which needs no device, so a
        misspelled name is a validation error even when the bulb has never been
        reachable. When capabilities are known, the name is also narrowed to the
        scenes this particular bulb supports.
        """
        wanted = name.strip().casefold()
        canonical = next(
            (c for c in SCENE_NAME_TO_ID if c.casefold() == wanted), None
        )
        supported = (
            self.supported_scene_names() if self._capabilities is not None else None
        )

        if canonical is not None and (supported is None or canonical in supported):
            return SCENE_NAME_TO_ID[canonical]

        if supported:
            raise ValidationError(
                f"Unknown scene {name!r}. Supported scenes: {', '.join(supported)}."
            )
        raise ValidationError(
            f"Unknown scene {name!r}. It is not a WiZ scene name; call list_scenes "
            f"once the bulb at {self.address} is reachable to see what it supports."
        )

    def require_feature(self, attribute: str, description: str) -> None:
        """Raise if the bulb reports it does not support `attribute`."""
        features = self.require_capabilities().features
        if not getattr(features, attribute, False):
            raise BulbUnsupportedOperation(
                f"The device at {self.address} does not support {description}."
            )

    # --- device access ----------------------------------------------------

    async def _invoke(self, what: str, awaitable):
        """Run one device call with a timeout, mapping failures to BulbError.

        Caller must already hold the lock. `WizLightError` is caught last because
        every other pywizlight exception derives from it.
        """
        try:
            return await asyncio.wait_for(awaitable, timeout=self._timeout)
        except asyncio.TimeoutError:
            raise BulbTimeout(
                f"The bulb at {self.address} did not respond within "
                f"{self._timeout:g}s while trying to {what}."
            ) from None
        except WizLightTimeOutError:
            raise BulbTimeout(
                f"The bulb at {self.address} did not respond while trying to {what}."
            ) from None
        except WizLightConnectionError as exc:
            # pywizlight raises this for two unrelated causes, but distinguishes them
            # in the exception chain: the network case is `raise ... from ex` on an
            # OSError, while a device error payload is raised unchained. So the cause
            # tells us which happened.
            if isinstance(exc.__cause__, OSError):
                raise BulbUnreachable(
                    f"The bulb at {self.address} is unreachable on the network "
                    f"while trying to {what}: {exc}"
                ) from None
            raise BulbRejected(
                f"The bulb at {self.address} refused the request to {what}: {exc}. "
                f"The device is reachable but rejected the command; on recent WiZ "
                f"firmware this is usually because writes must be signed, which "
                f"pywizlight does not implement (see its issue #213). Fix: turn off "
                f"message signing for this bulb in the WiZ app; reads keep working "
                f"either way."
            ) from None
        except WizLightNotKnownBulb:
            raise BulbUnknownModel(
                f"The device at {self.address} is not a recognised model, "
                f"so it cannot {what}."
            ) from None
        except WizLightMethodNotFound:
            raise BulbUnsupportedOperation(
                f"The device at {self.address} rejected an unsupported operation "
                f"while trying to {what}."
            ) from None
        except WizLightError as exc:  # base class: must come last
            raise BulbError(
                f"The bulb at {self.address} reported an error while trying to "
                f"{what}: {exc}"
            ) from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Unexpected failure while trying to %s", what)
            raise BulbInternalError(
                f"An internal error occurred while trying to {what} "
                f"on the bulb at {self.address}: {exc}"
            ) from None

    def _mark_reachable(self, reachable: bool) -> None:
        if reachable != self._reachable:
            log.info(
                "Bulb at %s is now %s",
                self.address,
                "reachable" if reachable else "not reachable",
            )
        self._reachable = reachable

    async def _refresh_locked(self) -> Snapshot:
        """Read state into the cache. Caller holds the lock."""
        try:
            # updateState() returns List[Optional[PilotParser]] (dual-head support),
            # so the single-head state is at index 0. pywizlight's README shows
            # `bulb.state.get_state()`, which does not work on the shipped code.
            pilots = await self._invoke("read state", self._light.updateState())
        except BulbError:
            self._mark_reachable(False)
            raise

        parser = pilots[0] if pilots else None
        if parser is None:
            self._mark_reachable(False)
            raise BulbError(f"The bulb at {self.address} returned no state.")

        raw_brightness = parser.get_brightness()
        # A white-only bulb, or one currently in white mode, reports (None, None, None)
        # rather than omitting the colour. Normalise that to "no colour".
        rgb = parser.get_rgb()
        if rgb is not None and all(channel is None for channel in rgb):
            rgb = None
        self._state = LightState(
            on=bool(parser.get_state()),
            brightness_pct=(
                raw_to_pct(raw_brightness) if raw_brightness is not None else None
            ),
            rgb=rgb,
            color_temp_kelvin=parser.get_colortemp(),
            scene=parser.get_scene(),
        )
        self._last_confirmed = datetime.now(timezone.utc)
        self._mark_reachable(True)

        if self._capabilities is None:
            # Fetched once and cached for the process lifetime: this is what gives
            # the real Kelvin range and feature flags used to validate input.
            self._capabilities = await self._invoke(
                "read capabilities", self._light.get_bulbtype()
            )

        return self.snapshot()

    async def refresh(self) -> Snapshot:
        async with self._lock:
            return await self._refresh_locked()

    async def _write_locked(self, what: str, awaitable) -> Snapshot:
        """Perform a write, then refresh the cache under the same lock.

        Holding the lock across both keeps a poll from landing between the write
        and the cache update. A refresh failure does not fail the write, which
        already succeeded.
        """
        await self._invoke(what, awaitable)
        try:
            return await self._refresh_locked()
        except BulbError:
            return self.snapshot()

    async def apply(self, builder: PilotBuilder, what: str = "set state") -> Snapshot:
        """Apply an appearance to the bulb.

        Uses `turn_on`, which sends the protocol's `setPilot`. pywizlight's
        `set_state` sends `setState` instead, which WiZ bulbs do not implement --
        the library's own device fixtures have no handler for it. The consequence
        is deliberate: applying an appearance also switches the light on, because
        there is no supported way to change appearance without doing so.
        """
        async with self._lock:
            return await self._write_locked(what, self._light.turn_on(builder))

    async def turn_on(self, builder: PilotBuilder | None = None) -> Snapshot:
        async with self._lock:
            return await self._write_locked(
                "turn on", self._light.turn_on(builder or PilotBuilder())
            )

    async def turn_off(self) -> Snapshot:
        async with self._lock:
            return await self._write_locked("turn off", self._light.turn_off())

    # --- lifecycle --------------------------------------------------------

    async def poll_forever(self) -> None:
        """Refresh the cache on the poll interval, forever.

        Never exits on a device failure: the bulb being powered off is the normal
        case this loop exists to detect, and it must keep running to notice it
        coming back.
        """
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except BulbError as exc:
                log.debug("Poll failed: %s", exc)
            except Exception:
                log.exception("Unexpected error in poll loop; continuing")
            await asyncio.sleep(self._poll_interval)

    async def close(self) -> None:
        await self._light.async_close()
