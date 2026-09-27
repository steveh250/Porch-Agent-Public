#!/usr/bin/env python3
"""Standalone test harness: drives a running server against a REAL bulb.

This is an operator tool, not part of the server. It speaks MCP over
streamable-HTTP as an ordinary client, so what it validates is the same contract
an agent would use. It deliberately imports nothing from `porch_agent` and never
talks to the bulb directly.

Usage:
    1. Start the server in one terminal:   porch-agent
    2. Run this in another:                python harness.py

    python harness.py --url http://192.168.1.10:8000/mcp   # non-default server
    python harness.py --pause 3                            # linger on each change

Exit status: 0 if every check passed, non-zero otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from typing import Any

from dotenv import load_dotenv
from mcp import Client

EXPECTED_TOOLS = (
    "get_light_state",
    "turn_on",
    "turn_off",
    "set_brightness",
    "set_color",
    "set_color_temp",
    "set_scene",
    "list_scenes",
)

EXIT_OK = 0
EXIT_CHECKS_FAILED = 1
EXIT_NO_SERVER = 2
EXIT_BULB_UNREACHABLE = 3

DEFAULT_SCENE_SAMPLE = 5

# "Custom Mode N" are user-defined slots that are empty unless you have filled
# them in the WiZ app, so setting one proves nothing and may simply fail. They are
# excluded from the random sample rather than producing failures that are not bugs.
EXCLUDED_SCENE_PREFIXES = ("Custom Mode",)


def choose_scenes(scene_names: list[str], count: int) -> list[str]:
    """Pick a random sample of real, settable scenes from what the bulb reports."""
    usable = [
        name
        for name in scene_names
        if not name.startswith(EXCLUDED_SCENE_PREFIXES)
    ]
    return random.sample(usable, min(count, len(usable)))


def default_url() -> str:
    """Build the server URL from the same .env the server uses."""
    load_dotenv(".env", override=False)
    host = os.environ.get("MCP_HOST", "").strip() or "127.0.0.1"
    port = os.environ.get("MCP_PORT", "").strip() or "8000"
    path = os.environ.get("MCP_PATH", "").strip() or "/mcp"
    if not path.startswith("/"):
        path = "/" + path
    # A server bound to all interfaces is reached over loopback from this machine.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{port}{path}"


class Report:
    """Per-check pass/fail, printed as it goes and summarised at the end."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, label: str, ok: bool, detail: str = "") -> None:
        self.rows.append((label, ok, detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {label}"
        if detail:
            line += f" -- {detail}"
        print(line, flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)

    def summary(self) -> None:
        passed = len(self.rows) - self.failed
        print("\n" + "=" * 62)
        print(f"  {passed} passed, {self.failed} failed, {len(self.rows)} checks total")
        if self.failed:
            print("\n  Failures:")
            for label, ok, detail in self.rows:
                if not ok:
                    print(f"    - {label}: {detail}")
        print("=" * 62)


def result_text(res: Any) -> str:
    return " ".join(c.text for c in res.content if getattr(c, "text", None))


def payload(res: Any) -> dict[str, Any]:
    try:
        return json.loads(result_text(res))
    except (json.JSONDecodeError, TypeError):
        return {}


async def call(cl: Client, report: Report, label: str, tool: str, args: dict | None = None):
    """Call a tool, record pass/fail, and return the parsed payload or None."""
    res = await cl.call_tool(tool, args or {})
    if res.is_error:
        report.add(label, False, result_text(res)[:160])
        return None
    data = payload(res)
    state = data.get("state") or {}
    detail = (
        f"on={state.get('on')} bright={state.get('brightness_pct')}%"
        if "state" in data
        else f"{len(data.get('scenes', []))} scenes"
    )
    report.add(label, True, detail)
    return data


async def expect_error(cl: Client, report: Report, label: str, tool: str, args: dict):
    """A call that SHOULD be rejected. Passes only if it is."""
    res = await cl.call_tool(tool, args)
    if res.is_error:
        report.add(label, True, result_text(res)[:120])
    else:
        report.add(label, False, "expected rejection, but the call succeeded")


def restore_args(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Work out the call that puts the light back how we found it."""
    if not state.get("on"):
        return "turn_off", {}
    args: dict[str, Any] = {}
    if state.get("brightness_pct") is not None:
        args["brightness_pct"] = state["brightness_pct"]
    rgb = state.get("rgb")
    temp = state.get("color_temp_kelvin")
    if rgb and any(rgb):
        args["red"], args["green"], args["blue"] = rgb
    elif temp:
        args["color_temp_kelvin"] = temp
    return "turn_on", args


async def run(url: str, pause: float, scene_sample: int) -> int:
    report = Report()
    print(f"Connecting to {url}")

    try:
        client_cm = Client(url)
        cl = await client_cm.__aenter__()
    except Exception as exc:
        # Distinct from a bulb fault: the corrective action is different.
        print(f"\nERROR: could not connect to the MCP server at {url}")
        print(f"       {type(exc).__name__}: {exc}")
        print("       Is the server running? Start it with: porch-agent")
        return EXIT_NO_SERVER

    original: dict[str, Any] | None = None
    try:
        print("\nTool list")
        advertised = {t.name for t in (await cl.list_tools()).tools}
        for name in EXPECTED_TOOLS:
            report.add(
                f"tool advertised: {name}",
                name in advertised,
                "" if name in advertised else "MISSING from the server's tool list",
            )
        extra = advertised - set(EXPECTED_TOOLS)
        if extra:
            report.add("no unexpected tools", False, f"unexpected: {sorted(extra)}")
        else:
            report.add("no unexpected tools", True)

        print("\nBulb reachability")
        state_doc = await call(cl, report, "get_light_state", "get_light_state")
        if state_doc is None:
            return EXIT_CHECKS_FAILED
        if not state_doc.get("reachable"):
            print(
                f"\nERROR: the server at {url} is reachable, but it reports the BULB "
                "as unreachable."
            )
            print("       Is the bulb powered on? Is WIZ_BULB_IP correct?")
            print(f"       ever_confirmed={state_doc.get('ever_confirmed')}")
            report.summary()
            return EXIT_BULB_UNREACHABLE
        report.add("bulb reachable", True)
        original = state_doc.get("state") or {}
        print(f"\n  Recorded original state: {original}")

        print("\nVisible changes (watch the light)")
        await call(cl, report, "turn_on", "turn_on")
        await asyncio.sleep(pause)
        await call(cl, report, "set_brightness 100%", "set_brightness", {"brightness_pct": 100})
        await asyncio.sleep(pause)
        await call(cl, report, "set_brightness 20%", "set_brightness", {"brightness_pct": 20})
        await asyncio.sleep(pause)

        scenes_doc = await call(cl, report, "list_scenes", "list_scenes")
        scene_names = (scenes_doc or {}).get("scenes", [])

        await call(cl, report, "get_light_state (mid-run)", "get_light_state")

        await call(cl, report, "set_color red", "set_color", {"red": 255, "green": 0, "blue": 0})
        await asyncio.sleep(pause)
        await call(cl, report, "set_color blue", "set_color", {"red": 0, "green": 0, "blue": 255})
        await asyncio.sleep(pause)
        await call(cl, report, "set_color_temp 2700K", "set_color_temp", {"color_temp_kelvin": 2700})
        await asyncio.sleep(pause)

        if scene_names:
            sample = choose_scenes(scene_names, scene_sample)
            print(f"\n  Scene sample ({len(sample)} of "
                  f"{len(scene_names)} advertised): {', '.join(sample)}")
            for scene in sample:
                await call(cl, report, f"set_scene {scene!r}", "set_scene",
                           {"name": scene})
                await asyncio.sleep(pause)
            # One of the sampled scenes again in lower case: the tool is specified to
            # match names without regard to case.
            lowered = sample[0].lower()
            await call(cl, report, f"set_scene {lowered!r} (case-insensitive)",
                       "set_scene", {"name": lowered})
            await asyncio.sleep(pause)
        else:
            report.add("set_scene", False, "bulb reported no supported scenes")

        print("\nRejections (these SHOULD fail)")
        await expect_error(cl, report, "set_brightness 150 rejected",
                           "set_brightness", {"brightness_pct": 150})
        await expect_error(cl, report, "set_color 300 rejected",
                           "set_color", {"red": 300, "green": 0, "blue": 0})
        await expect_error(cl, report, "set_scene 'Nonexistent' rejected",
                           "set_scene", {"name": "Nonexistent"})

        await call(cl, report, "turn_off", "turn_off")
        await asyncio.sleep(pause)
    finally:
        # Restore whatever we found, on the success and the failure path alike.
        if original is not None:
            tool, args = restore_args(original)
            print(f"\nRestoring original state via {tool}({args})")
            try:
                res = await cl.call_tool(tool, args)
                report.add("original state restored", not res.is_error,
                           result_text(res)[:120] if res.is_error else "")
            except Exception as exc:
                report.add("original state restored", False, f"{type(exc).__name__}: {exc}")
        try:
            await client_cm.__aexit__(None, None, None)
        except Exception:
            pass

    report.summary()
    return EXIT_OK if report.failed == 0 else EXIT_CHECKS_FAILED


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=None,
                        help="MCP endpoint URL (default: built from .env / MCP_* vars)")
    parser.add_argument("--pause", type=float, default=1.5,
                        help="seconds to linger on each visible change (default 1.5)")
    parser.add_argument("--scene-sample", type=int, default=DEFAULT_SCENE_SAMPLE,
                        help=f"how many random scenes to cycle through "
                             f"(default {DEFAULT_SCENE_SAMPLE})")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed the scene sample, to repeat a particular run")
    ns = parser.parse_args()
    if ns.scene_sample < 1:
        parser.error("--scene-sample must be at least 1")
    seed = ns.seed if ns.seed is not None else random.randrange(1_000_000)
    random.seed(seed)
    print(f"Scene sample seed: {seed}  (repeat this run with --seed {seed})")
    url = ns.url or default_url()
    try:
        return asyncio.run(run(url, ns.pause, ns.scene_sample))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return EXIT_CHECKS_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
