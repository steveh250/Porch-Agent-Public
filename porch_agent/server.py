"""Server construction and startup.

The server always starts, whether or not the bulb answers: an unreachable bulb is
a normal running state, not a startup failure. Only a configuration mistake --
a missing bulb address or a malformed number -- stops it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .bulb import BulbController
from .config import ConfigError, Settings, load_settings
from .tools import AppContext, register_tools

log = logging.getLogger("porch_agent")

INSTRUCTIONS = (
    "Controls a single WiZ smart bulb on the local network. The bulb may be "
    "powered off at the wall, in which case get_light_state reports it as not "
    "reachable and write operations fail -- this is expected, not a fault."
)


def build_server(settings: Settings) -> MCPServer:
    """Construct the server, wiring the bulb connection into its lifespan."""

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
        bulb = BulbController(
            ip=settings.bulb_ip,
            port=settings.bulb_port,
            request_timeout_seconds=settings.request_timeout_seconds,
            poll_interval_seconds=settings.poll_interval_seconds,
        )
        # Lazy connect: nothing is sent to the bulb here, so an unpowered bulb
        # cannot prevent startup. The poll loop discovers it when it appears.
        poll = asyncio.create_task(bulb.poll_forever(), name="bulb-poll")
        log.info(
            "Polling bulb at %s every %gs", bulb.address, settings.poll_interval_seconds
        )
        try:
            yield AppContext(bulb=bulb)
        finally:
            poll.cancel()
            try:
                await poll
            except asyncio.CancelledError:
                pass
            await bulb.close()
            log.info("Bulb connection closed")

    mcp = MCPServer(name="porch-agent", instructions=INSTRUCTIONS, lifespan=lifespan)
    register_tools(mcp)
    return mcp


def _log_startup(settings: Settings) -> None:
    log.info(
        "Serving MCP over streamable-HTTP on http://%s:%d%s",
        settings.mcp_host,
        settings.mcp_port,
        settings.mcp_path,
    )
    log.info("Bulb address: %s", settings.bulb_address)
    log.info("Poll interval: %gs", settings.poll_interval_seconds)
    log.info("Request timeout: %gs", settings.request_timeout_seconds)
    if settings.needs_allowed_hosts:
        log.warning(
            "MCP_HOST is %s (not loopback) but MCP_ALLOWED_HOSTS is empty. "
            "DNS-rebinding protection is enabled, so requests from other machines "
            "will be REJECTED on their Host header. Set MCP_ALLOWED_HOSTS to the "
            "host values clients connect as, for example "
            "MCP_ALLOWED_HOSTS=%s:%d",
            settings.mcp_host,
            settings.mcp_host,
            settings.mcp_port,
        )


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """Build Host-header protection settings.

    Neither obvious choice works here. Passing no settings at all disables
    DNS-rebinding protection entirely -- the SDK's middleware reads `None` as
    "off" for backwards compatibility. Passing settings with an empty allow-list
    rejects *every* request, loopback included, with 421 Misdirected Request.

    So when nothing is configured we allow loopback explicitly: protection stays
    on, local clients work, and reaching the server from another machine requires
    setting MCP_ALLOWED_HOSTS -- which is what the startup warning is about.
    """
    hosts = list(settings.mcp_allowed_hosts)
    if not hosts:
        hosts = [
            f"127.0.0.1:{settings.mcp_port}",
            f"localhost:{settings.mcp_port}",
            "127.0.0.1",
            "localhost",
        ]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[])


def main() -> int:
    """Console entry point."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    try:
        settings = load_settings()
    except ConfigError as exc:
        # Configuration errors are fatal; an unreachable bulb is not.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    _log_startup(settings)
    mcp = build_server(settings)

    security = _transport_security(settings)
    try:
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_host,
            port=settings.mcp_port,
            streamable_http_path=settings.mcp_path,
            transport_security=security,
        )
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
