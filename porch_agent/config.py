"""Settings for the server, read from the environment with `.env` as a fallback.

Values already present in the process environment always win over the file, so a
deployment can override any setting without editing `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = ".env"
EXAMPLE_FILE = ".env.example"

# pywizlight's actual default port. Its README documents 12345, which does not
# match the shipped code -- do not "correct" this to 12345.
DEFAULT_BULB_PORT = 38899
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000
DEFAULT_MCP_PATH = "/mcp"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    """Effective configuration for one server process."""

    bulb_ip: str
    bulb_port: int
    poll_interval_seconds: float
    request_timeout_seconds: float
    mcp_host: str
    mcp_port: int
    mcp_path: str
    mcp_allowed_hosts: tuple[str, ...]

    @property
    def bulb_address(self) -> str:
        """Human-readable bulb address, included in every device error."""
        return f"{self.bulb_ip}:{self.bulb_port}"

    @property
    def binds_loopback(self) -> bool:
        return self.mcp_host in LOOPBACK_HOSTS

    @property
    def needs_allowed_hosts(self) -> bool:
        """True when bound off-loopback with no allow-list, so remote requests fail."""
        return not self.binds_loopback and not self.mcp_allowed_hosts


def _require(name: str) -> str:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise ConfigError(
            f"{name} is not set. Add it to {ENV_FILE} in the project root "
            f"(copy {EXAMPLE_FILE} to get started), or set it in the environment."
        )
    return raw


def _positive_number(name: str, default: float, cast) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{name} must be a positive number, but is {raw!r}."
        ) from None
    if value <= 0:
        raise ConfigError(f"{name} must be a positive number, but is {raw!r}.")
    return value


def _text(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def load_settings(env_file: str | os.PathLike[str] = ENV_FILE) -> Settings:
    """Load settings, reading `env_file` for any variable not already in the environment.

    Raises ConfigError when the bulb address is absent or a numeric setting is
    not a positive number. Those are configuration mistakes, so they fail
    startup -- unlike an unreachable bulb, which is a normal running state.
    """
    path = Path(env_file)
    if path.is_file():
        # override=False: real environment variables take precedence over the file.
        load_dotenv(path, override=False)

    bulb_ip = _require("WIZ_BULB_IP")
    bulb_port = int(_positive_number("WIZ_BULB_PORT", DEFAULT_BULB_PORT, int))
    poll_interval = _positive_number(
        "WIZ_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS, float
    )
    request_timeout = _positive_number(
        "WIZ_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS, float
    )

    mcp_host = _text("MCP_HOST", DEFAULT_MCP_HOST)
    mcp_port = int(_positive_number("MCP_PORT", DEFAULT_MCP_PORT, int))
    mcp_path = _text("MCP_PATH", DEFAULT_MCP_PATH)
    if not mcp_path.startswith("/"):
        mcp_path = "/" + mcp_path

    allowed = tuple(
        part.strip()
        for part in _text("MCP_ALLOWED_HOSTS", "").split(",")
        if part.strip()
    )

    return Settings(
        bulb_ip=bulb_ip,
        bulb_port=bulb_port,
        poll_interval_seconds=poll_interval,
        request_timeout_seconds=request_timeout,
        mcp_host=mcp_host,
        mcp_port=mcp_port,
        mcp_path=mcp_path,
        mcp_allowed_hosts=allowed,
    )
