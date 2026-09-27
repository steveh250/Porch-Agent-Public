"""Command line: ``porch-light run`` and ``porch-light simulate FILE``."""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .agent import make_local_light_tools, run_agent
from .arrival import ArrivalDeps, default_deps, handle_arrival
from .config import Config, ConfigError, load_config
from .lightctl import FakeLight
from .monitor import load_fixes, run_mqtt, run_simulation
from .resting import RestingLight

log = logging.getLogger("porch_light")


def _parse_at(value: str, config: Config) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.home.tz)
    return moment


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="porch-light", description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="subscribe to OwnTracks over MQTT and run forever")

    sim = sub.add_parser("simulate", help="feed scripted location fixes from a file")
    sim.add_argument("file", type=Path, help="one OwnTracks JSON payload per line")
    sim.add_argument("--fake-light", action="store_true",
                     help="use an in-memory bulb instead of the MCP server")
    sim.add_argument("--force-fallback", action="store_true",
                     help="skip weather and the LLM; exercise the fallback path")
    sim.add_argument("--at", metavar="ISO_TIME",
                     help="pretend it is this time (e.g. 2026-11-20T21:30); "
                          "naive times are in the home timezone")
    sim.add_argument("--delay", type=float, default=0.0,
                     help="seconds to pause between fixes")
    return parser


async def _simulate(args: argparse.Namespace, config: Config) -> int:
    deps: ArrivalDeps = default_deps(config)
    fake: FakeLight | None = None
    if args.fake_light:
        fake = FakeLight((config.bulb.kelvin_min, config.bulb.kelvin_max))
        deps.light_factory = lambda: fake
        deps.agent_runner = functools.partial(run_agent, light_tools=make_local_light_tools(fake))
    if args.at:
        at = _parse_at(args.at, config)
        deps.clock = lambda: at

    records: list[dict] = []

    async def handler(distance_m: float) -> None:
        record = await handle_arrival(config, deps, distance_m, force_fallback=args.force_fallback)
        records.append(record)
        summary = {k: record[k] for k in
                   ("decision", "reason", "decided_by", "fallback_used", "verification", "errors")}
        print(json.dumps(summary, indent=2, default=str))

    print(f"Simulating {args.file} (home {config.home.lat}, {config.home.lon}; "
          f"outer {config.presence.outer_radius_m:g} m, inner {config.presence.inner_radius_m:g} m)")
    await run_simulation(config, handler, load_fixes(args.file), args.delay)
    print(f"{len(records)} arrival(s) handled; log: {config.event_log}")
    if fake is not None:
        print(f"Fake bulb final state: on={fake.on} brightness={fake.brightness_pct}% "
              f"kelvin={fake.color_temp_kelvin}K")
    return 0


async def _run(config: Config) -> int:
    deps = default_deps(config)
    # One lock for everything that writes to the bulb, so the resting light
    # never changes it in the middle of an arrival.
    lock = asyncio.Lock()
    resting = RestingLight(config, deps, lock) if config.resting.enabled else None

    async def handler(distance_m: float) -> None:
        async with lock:
            record = await handle_arrival(config, deps, distance_m)
        if resting is not None:
            resting.note_arrival(record)

    log.info("Home at %.5f, %.5f; outer %g m, inner %g m; model %s; MCP %s",
             config.home.lat, config.home.lon, config.presence.outer_radius_m,
             config.presence.inner_radius_m, config.llm.model, config.mcp.url)
    if not config.llm.api_key:
        log.warning("OPENROUTER_API_KEY is not set: every arrival will use the fallback.")
    if not config.weather.api_key:
        log.warning("OWM_API_KEY is not set: every arrival will use the fallback.")
    if resting is None:
        await run_mqtt(config, handler)
        return 0
    r = config.resting
    log.info("Resting light: %d %% / %d K from civil dusk to dawn, off in daylight; "
             "back to resting %g min after an arrival", r.brightness_pct,
             r.color_temp_kelvin, r.revert_after_min)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(resting.run_forever(), name="resting-light")
        tasks.create_task(run_mqtt(config, handler), name="mqtt")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "simulate":
            return asyncio.run(_simulate(args, config))
        return asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
