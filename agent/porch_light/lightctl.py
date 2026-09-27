"""Direct, deterministic access to the porch-agent MCP server.

This is the path that does not involve the LLM: it reads state before an
arrival, verifies the result afterwards, and applies the fallback. It speaks
MCP over streamable-HTTP like any other client.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class LightError(RuntimeError):
    """The server could not be reached, or a tool call failed."""


class LightClient(Protocol):
    async def get_state(self) -> dict[str, Any]: ...
    async def turn_on(
        self, brightness_pct: int | None = None, color_temp_kelvin: int | None = None
    ) -> dict[str, Any]: ...
    async def turn_off(self) -> dict[str, Any]: ...


def _result_doc(result: Any) -> dict[str, Any]:
    """Extract the tool's dict result from a CallToolResult."""
    texts = [getattr(c, "text", "") for c in (result.content or []) if getattr(c, "type", "") == "text"]
    if result.isError:
        raise LightError("; ".join(t for t in texts if t) or "tool call failed")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        # Tools whose return type is not an object are wrapped as {"result": ...}.
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for text in texts:
        try:
            doc = json.loads(text)
        except ValueError:
            continue
        if isinstance(doc, dict):
            return doc
    raise LightError("tool returned no structured result")


class McpLightClient:
    """Async context manager holding one MCP session to the server."""

    def __init__(self, url: str, timeout_s: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout_s
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpLightClient":
        stack = AsyncExitStack()
        try:
            http = await stack.enter_async_context(httpx.AsyncClient(timeout=self._timeout))
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(self._url, http_client=http)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException as exc:
            await stack.aclose()
            if isinstance(exc, Exception):
                raise LightError(f"cannot reach MCP server at {self._url}: {exc}") from exc
            raise
        self._stack, self._session = stack, session
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = self._session = None

    async def _call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._session is None:
            raise LightError("session is not open")
        try:
            result = await self._session.call_tool(name, args or {})
        except LightError:
            raise
        except Exception as exc:
            raise LightError(f"{name} failed: {exc}") from exc
        return _result_doc(result)

    async def get_state(self) -> dict[str, Any]:
        return await self._call("get_light_state")

    async def turn_on(
        self, brightness_pct: int | None = None, color_temp_kelvin: int | None = None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if brightness_pct is not None:
            args["brightness_pct"] = brightness_pct
        if color_temp_kelvin is not None:
            args["color_temp_kelvin"] = color_temp_kelvin
        return await self._call("turn_on", args)

    async def turn_off(self) -> dict[str, Any]:
        return await self._call("turn_off")


class FakeLight:
    """In-memory stand-in for the server, for ``simulate --fake-light`` and tests.

    Mimics the server's documents and its validation: Kelvin outside the range
    and brightness outside 0-100 are rejected, as the real server does.
    """

    def __init__(self, kelvin_range: tuple[int, int] = (2200, 6500), reachable: bool = True) -> None:
        self.kelvin_range = kelvin_range
        self.reachable = reachable
        self.on = False
        self.brightness_pct: int | None = 50
        self.color_temp_kelvin: int | None = 4000
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "FakeLight":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def _doc(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "ever_confirmed": True,
            "last_confirmed": None,
            "state": {
                "on": self.on,
                "brightness_pct": self.brightness_pct,
                "rgb": None,
                "color_temp_kelvin": self.color_temp_kelvin,
                "scene": None,
            },
            "kelvin_range": {"min": self.kelvin_range[0], "max": self.kelvin_range[1]},
        }

    async def get_state(self) -> dict[str, Any]:
        self.calls.append(("get_light_state", {}))
        return self._doc()

    async def turn_on(
        self, brightness_pct: int | None = None, color_temp_kelvin: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(("turn_on", {"brightness_pct": brightness_pct,
                                       "color_temp_kelvin": color_temp_kelvin}))
        if not self.reachable:
            raise LightError("[unreachable] bulb is unreachable")
        if brightness_pct is not None and not 0 <= brightness_pct <= 100:
            raise LightError("[validation] brightness_pct out of range")
        lo, hi = self.kelvin_range
        if color_temp_kelvin is not None and not lo <= color_temp_kelvin <= hi:
            raise LightError("[validation] color_temp_kelvin out of range")
        self.on = True
        if brightness_pct is not None:
            self.brightness_pct = brightness_pct
        if color_temp_kelvin is not None:
            self.color_temp_kelvin = color_temp_kelvin
        return self._doc()

    async def turn_off(self) -> dict[str, Any]:
        self.calls.append(("turn_off", {}))
        if not self.reachable:
            raise LightError("[unreachable] bulb is unreachable")
        self.on = False
        return self._doc()
