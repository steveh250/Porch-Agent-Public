"""The decision agent (Microsoft Agent Framework).

The agent sees three kinds of tools: the sun, the weather, and the bulb's MCP
tools (``get_light_state``, ``turn_on``, ``turn_off`` only). Every bulb call
passes through ``ClampMiddleware`` first, so whatever the model asks for, the
bulb only ever receives values inside the configured limits.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_framework import (
    Agent,
    FunctionInvocationContext,
    FunctionMiddleware,
    MCPStreamableHTTPTool,
    tool,
)

from .config import BulbConfig, Config
from .guardrails import Decision, KelvinRange, clamp_brightness, clamp_kelvin, parse_decision

log = logging.getLogger(__name__)

LIGHT_TOOLS = ("get_light_state", "turn_on", "turn_off")
MAX_ITERATIONS = 8

# Appended to policy.md in code, because the rest of the system depends on it:
# policy.md says *what* to choose, this says *how* to act and report.
PROTOCOL = """
## Operating protocol (fixed; not part of the editable policy)

1. Call get_sun_context and get_weather to understand the situation.
2. Decide on: on or off, brightness (10-100 %) and colour temperature (Kelvin).
   If the policy says to leave the light alone, make no bulb calls.
3. Apply the decision with exactly one call: turn_on(brightness_pct=...,
   color_temp_kelvin=...) or turn_off(). Do not set colour (RGB) or scenes.
4. Call get_light_state and check the result matches your decision
   (brightness may differ by 1-2 %). If it does not match, repeat the set call
   once, then read the state again.
5. Finish with ONLY a JSON object, no other text:
   {"action": "on" | "off" | "none", "brightness_pct": <int or null>,
    "color_temp_kelvin": <int or null>, "verified": <true|false>,
    "reason": "<one sentence explaining the choice>"}
"""


class AgentError(RuntimeError):
    """The agent did not produce a usable decision."""


@dataclass
class ToolCallRecord:
    tool: str
    requested: dict[str, Any]
    sent: dict[str, Any] | None
    ok: bool | None = None
    error: str | None = None


@dataclass
class AgentOutcome:
    decision: Decision
    reason: str
    self_verified: bool | None
    tool_calls: list[ToolCallRecord]
    raw_text: str
    duration_s: float


class ClampMiddleware(FunctionMiddleware):
    """Clamp or refuse every bulb tool call before it reaches the MCP server."""

    def __init__(self, bulb: BulbConfig, kelvin_range: KelvinRange) -> None:
        self.bulb = bulb
        self.kelvin_range = kelvin_range
        self.calls: list[ToolCallRecord] = []

    def sanitise(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Arguments to actually send, or None if the call is not permitted."""
        if name == "turn_on":
            sent: dict[str, Any] = {}
            brightness = clamp_brightness(args.get("brightness_pct"), self.bulb)
            kelvin = clamp_kelvin(args.get("color_temp_kelvin"), self.kelvin_range)
            if brightness is not None:
                sent["brightness_pct"] = brightness
            if kelvin is not None:
                sent["color_temp_kelvin"] = kelvin
            # red/green/blue are dropped deliberately: the porch light is white.
            return sent
        if name in ("turn_off", "get_light_state"):
            return {}
        return None

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        name = context.function.name
        if name not in LIGHT_TOOLS:
            if name in ("get_sun_context", "get_weather"):
                await call_next()
                return
            # Anything else is refused without reaching the server.
            self.calls.append(ToolCallRecord(name, dict(context.arguments or {}), None, False,
                                             "not permitted"))
            context.result = f"Error: tool {name} is not permitted for this task."
            return

        requested = dict(context.arguments or {})
        sent = self.sanitise(name, requested)
        record = ToolCallRecord(name, requested, sent)
        self.calls.append(record)
        if sent is None:
            record.ok, record.error = False, "not permitted"
            context.result = f"Error: tool {name} is not permitted for this task."
            return
        if sent != requested:
            log.info("Clamped %s arguments %s -> %s", name, requested, sent)
        context.arguments = sent
        try:
            await call_next()
        except Exception as exc:
            record.ok, record.error = False, str(exc)
            raise
        record.ok = True


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_final(text: str) -> dict[str, Any] | None:
    """Pull the JSON object out of the agent's final message (fences tolerated)."""
    match = _JSON_OBJECT.search(text or "")
    if not match:
        return None
    try:
        doc = json.loads(match.group(0))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def load_instructions(policy_file: Path) -> str:
    return policy_file.read_text().strip() + "\n\n" + PROTOCOL.strip() + "\n"


def build_prompt(distance_m: float, sun: dict[str, Any]) -> str:
    return (
        f"Arrival event: the phone has just come within {distance_m:.0f} m of home "
        f"and is approaching. Local time is {sun['local_time']}. Decide how the "
        f"porch light should be set for this arrival, apply it, verify it, and "
        f"report as instructed."
    )


def make_situation_tools(sun: dict[str, Any], weather: dict[str, Any]) -> list[Any]:
    """Tools answering from the snapshot taken at the start of this arrival.

    The situation is fetched once by the deterministic layer (so it can be
    logged and so a weather failure triggers the fallback before any LLM call);
    the tools hand the agent the same values.
    """

    @tool(name="get_sun_context", approval_mode="never_require")
    def get_sun_context() -> dict[str, Any]:
        """Sun elevation and twilight phase (day / civil_twilight / night) at home right now,
        with today's civil dawn, sunrise, sunset and civil dusk times."""
        return sun

    @tool(name="get_weather", approval_mode="never_require")
    def get_weather() -> dict[str, Any]:
        """Current weather at home: condition, rain/snow/fog flags, cloud cover, visibility."""
        return weather

    return [get_sun_context, get_weather]


def make_chat_client(config: Config) -> Any:
    from agent_framework.openai import OpenAIChatCompletionClient

    if not config.llm.api_key:
        raise AgentError("OPENROUTER_API_KEY is not set.")
    # OpenRouter speaks the Chat Completions API, not OpenAI's Responses API,
    # so this is the Chat Completion client rather than OpenAIChatClient.
    return OpenAIChatCompletionClient(
        model=config.llm.model,
        api_key=config.llm.api_key,
        base_url=config.llm.base_url,
        default_headers={"X-Title": "porch-light"},
    )


async def run_agent(
    config: Config,
    *,
    distance_m: float,
    sun: dict[str, Any],
    weather: dict[str, Any],
    kelvin_range: KelvinRange,
    chat_client: Any | None = None,
    light_tools: list[Any] | None = None,
) -> AgentOutcome:
    """Run the agent once. Raises AgentError (or anything else) on failure.

    ``chat_client`` and ``light_tools`` are injectable for tests; by default the
    OpenRouter client and the server's MCP tools are used. The caller owns the
    overall timeout.
    """
    started = time.monotonic()
    client = chat_client if chat_client is not None else make_chat_client(config)
    invocation = getattr(client, "function_invocation_configuration", None)
    if isinstance(invocation, dict):
        # Let the model see why a bulb call failed (e.g. "[unreachable] ..."),
        # not just "Function failed.", so its reason can say so.
        invocation["include_detailed_errors"] = True
        # The protocol needs about five round trips; allow a retry's worth more.
        invocation["max_iterations"] = MAX_ITERATIONS
    clamp = ClampMiddleware(config.bulb, kelvin_range)

    async with AsyncExitStack() as stack:
        if light_tools is None:
            mcp_tool = MCPStreamableHTTPTool(
                name="porch_light",
                url=config.mcp.url,
                allowed_tools=list(LIGHT_TOOLS),
                load_prompts=False,
                approval_mode="never_require",
                request_timeout=int(config.mcp.timeout_s),
            )
            await stack.enter_async_context(mcp_tool)
            light_tools = [mcp_tool]

        agent = Agent(
            client,
            load_instructions(config.policy_file),
            name="porch_light",
            tools=[*make_situation_tools(sun, weather), *light_tools],
            middleware=[clamp],
            default_options={"temperature": 0.2},
        )
        response = await agent.run(build_prompt(distance_m, sun))

    text = response.text or ""
    final = parse_final(text)
    decision = parse_decision(final)
    if decision is None:
        raise AgentError(f"agent did not return a valid decision: {text[:300]!r}")

    reason = str(final.get("reason") or "").strip() if final else ""
    verified = final.get("verified") if final else None
    return AgentOutcome(
        decision=decision.clamped(config.bulb, kelvin_range),
        reason=reason or "(no reason given)",
        self_verified=verified if isinstance(verified, bool) else None,
        tool_calls=clamp.calls,
        raw_text=text,
        duration_s=round(time.monotonic() - started, 2),
    )


def make_local_light_tools(light: Any) -> list[Any]:
    """Bulb tools backed by a LightClient instead of the MCP server.

    Same names and arguments as the server's tools, so the agent and the clamp
    middleware cannot tell the difference. Used by ``simulate --fake-light``
    and the tests.
    """

    @tool(name="get_light_state", approval_mode="never_require")
    async def get_light_state() -> dict[str, Any]:
        """Report the light's state, whether it is reachable, and its Kelvin range."""
        return await light.get_state()

    @tool(name="turn_on", approval_mode="never_require")
    async def turn_on(
        brightness_pct: int | None = None, color_temp_kelvin: int | None = None
    ) -> dict[str, Any]:
        """Switch the light on, optionally setting brightness (%) and colour temperature (K)."""
        return await light.turn_on(brightness_pct, color_temp_kelvin)

    @tool(name="turn_off", approval_mode="never_require")
    async def turn_off() -> dict[str, Any]:
        """Switch the light off."""
        return await light.turn_off()

    return [get_light_state, turn_on, turn_off]
