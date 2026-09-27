# Proposal

## Why

A WiZ smart bulb is controllable only over an undocumented UDP protocol on the local
network, which puts it out of reach of an AI agent. Exposing the bulb through MCP turns
it into a set of tools an agent can call directly, so the porch light becomes something
an agent can reason about and operate rather than a device requiring a bespoke script.

The bulb is also frequently powered off at the wall. Any usable integration therefore has
to treat "unreachable" as a normal, expected state rather than an error condition — a naive
wrapper would stall every tool call for seconds against a dead bulb.

## What Changes

- Add a Python MCP server exposing one known WiZ bulb over **streamable-HTTP**, so the agent
  and the server can run on separate hosts.
- Depend on `pywizlight` (0.6.6, MIT) for the UDP protocol layer rather than reimplementing
  or forking it.
- Add eight MCP tools: `get_light_state`, `turn_on`, `turn_off`, `set_brightness`,
  `set_color`, `set_color_temp`, `set_scene`, `list_scenes`.
- Present an agent-friendly surface over pywizlight's raw API: brightness as **0-100 percent**
  (converted to the wire's 0-255), scenes selected **by name** rather than numeric `sceneId`,
  and colour temperature validated against the bulb's actual reported Kelvin range.
- Run a **background presence poll** on a configurable interval. Its most recent result is
  the state cache, so `get_light_state` answers immediately and reports whether the bulb is
  currently reachable instead of blocking on a UDP timeout.
- **Always start**, even when the bulb is unreachable: connect lazily and surface a clean,
  structured error from the first failing call rather than failing at startup.
- Configure entirely from a `.env` file: bulb address, poll interval, request timeout, and
  HTTP bind settings.
- Add a standalone Python **test harness** script that speaks MCP over streamable-HTTP to a
  running server and exercises every tool against real hardware. It is operated by hand, not
  by CI.
- Add a `.gitignore` (the repository currently has none).

## Capabilities

### New Capabilities

- `light-control`: The MCP tool surface for operating the bulb — the eight tools, their
  inputs and validation, the agent-facing units and naming, and the errors they return.
- `bulb-connection`: Lifecycle of the single bulb connection — lazy connect, the background
  presence poll, the state cache it populates, reachability tracking, and how pywizlight
  failures map to structured tool errors.
- `server-runtime`: How the server is configured and served — `.env` configuration, the
  streamable-HTTP endpoint and bind settings, host allow-listing, and always-start behaviour.
- `test-harness`: The standalone harness script — what it exercises, how it is invoked
  against a running server and real bulb, and what it reports.

### Modified Capabilities

None. This is the project's first change; there are no existing specs.

## Impact

- **New project structure.** The repository currently contains only OpenSpec scaffolding and
  no Python code, packaging, or `.gitignore`. This change introduces all of it.
- **Dependencies.** `pywizlight` and `mcp` (2.x). `uvicorn` and `starlette` arrive as hard
  dependencies of `mcp`, so streamable-HTTP needs no extra packages. `python-dotenv` must be
  declared explicitly, as it is only an extra of `mcp` rather than a hard dependency.
- **Python version.** `pywizlight` requires `>=3.11`. The development environment is 3.11.15,
  exactly at that floor, so the target runtime's version must be checked before deploying.
- **Network surface.** The server listens on HTTP and controls physical hardware. It ships
  unauthenticated, which is acceptable only on a trusted network; see non-goals.
- **No automated testing.** Validation is by hand, via the harness against a real bulb, on
  the machine the bulb is reachable from. Nothing in this change is exercised by CI.

## Non-Goals

Deliberately out of scope for v1, each recorded so it is not revisited by accident:

- **Bulb discovery** and **multi-bulb support.** One bulb, addressed by an IP from `.env`.
- **Authentication** on the HTTP endpoint.
- **Push updates.** `pywizlight` offers `start_push`, but a powered-off bulb pushes nothing,
  so polling is the correct mechanism for presence.
- **Automated tests against `pywizlight`'s bundled fake bulb.** `pywizlight.tests.fake_bulb`
  does ship in the wheel and works, making a future hardware-free test suite cheap — but the
  harness for v1 deliberately targets real hardware.
