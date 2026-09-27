# Design

## Context

See `proposal.md` for motivation. The repository is greenfield: it contains OpenSpec
scaffolding and nothing else — no Python package, no `pyproject.toml`, no `.gitignore`.

Constraints that shape the approach, all verified by running the libraries rather than reading
their documentation:

- **`pywizlight` 0.6.6's published documentation is materially wrong.** Its README documents a
  default port of `12345`, `bulb.state.get_state()`, `PilotBuilder(kelvin=...)` and `getMAC()`.
  The shipped code uses port **`38899`**, returns `List[Optional[PilotParser]]` from
  `updateState()` (indexed at `[0]`; the README's form raises `AttributeError`), takes
  `PilotBuilder(colortemp=...)`, and spells the method `getMac()`. Implementation must follow
  the code, not the README. Its `set_state()` is a related trap: the docstring implies a usable
  "set appearance without switching on", but it emits a `setState` message bulbs do not
  implement (see Decisions).
- **`mcp` 2.x removed `FastMCP`.** `mcp.server.fastmcp` raises `ModuleNotFoundError` pointing at
  a migration guide; the class is now `MCPServer`, from `mcp.server.mcpserver`. Almost every MCP
  example in circulation is 1.x `FastMCP` code and will not run.
- **`uvicorn` and `starlette` are already hard dependencies of `mcp`**, so streamable-HTTP adds
  no packages. **`python-dotenv` is not** — it sits behind `mcp`'s `cli` extra and must be
  declared directly.
- **Host-header protection has no safe default; it must be configured deliberately.**
  `TransportSecuritySettings` does default `enable_dns_rebinding_protection=True`, but that is
  not what a server gets by default, and neither obvious choice works:
  - Passing **no** transport settings **disables protection entirely** — the middleware reads
    `settings or TransportSecuritySettings(enable_dns_rebinding_protection=False)`, so `None`
    means off, for backwards compatibility.
  - Passing settings with an **empty `allowed_hosts`** rejects **every** request, loopback
    included, with `421 Misdirected Request`.

  So the server must pass settings that name the hosts it will accept, including loopback, or
  it is either unprotected or unreachable. (Corrected after implementation: an earlier version
  of this document described protection as simply "on by default", and the first
  implementation passed an empty allow-list and could not serve its own loopback clients.)
- **The bulb is often powered off**, and a UDP request to a dead bulb costs the full timeout.
- **No hardware is reachable from the development environment**, so nothing here can be
  validated locally beyond import and startup; the operator validates on the target machine.

## Goals / Non-Goals

**Goals:**

- Keep every device round trip off the read path, so tool calls stay fast when the bulb is off.
- Present device capabilities in the units and names an agent gets right on the first attempt.
- Make misconfiguration diagnosable from the startup log alone.
- Keep the harness a true client, so it validates the contract an agent would use.

**Non-Goals** (beyond the proposal's):

- No abstraction layer over `pywizlight` for hypothetical future device types. One bulb, one
  library, direct use.
- No persistence. Cached state lives in memory and is rebuilt by polling after a restart.
- No custom retry or backoff policy. The poll interval is the retry mechanism.

## Decisions

### Depend on `pywizlight` rather than fork or reimplement

The protocol layer encodes hardware quirks across firmware revisions and model families —
`protocol.py`, `bulblibrary.py`, the per-model config tables and the scene mappings. That is the
part least worth owning, and all of this change's value sits above it.

*Alternatives:* forking (inherits protocol maintenance for no gain, since no wire behaviour needs
changing); reimplementing the UDP protocol (weeks of work rediscovering undocumented quirks).

### Target `mcp` 2.x and `MCPServer`, not pinned 1.x

Writing against the current SDK avoids starting on a deprecated API. The cost is that online
examples don't apply, which is why the API facts above are recorded here.

*Alternative:* pin `mcp<2` to use `FastMCP` and match the examples — rejected as starting in
technical debt for a project with no legacy to preserve.

### `lifespan` owns the bulb connection and the poll task

`MCPServer` accepts a `lifespan` async context manager. It constructs the `wizlight` instance
and starts the poll task on entry, and cancels the task and closes the connection on exit. Tools
reach it through the request context rather than module-level globals.

This gives deterministic startup and shutdown, satisfying the spec's requirement that the
polling task stops cleanly and the process exits without hanging.

*Alternatives:* module-level singleton (untestable, no clean shutdown); connect per call
(a UDP timeout on every call against a dead bulb — exactly what the design is avoiding).

### One background poll, whose result *is* the cache

A single task loops on `WIZ_POLL_INTERVAL_SECONDS`, calling `updateState()` and storing the
result with a timestamp and a reachability flag. `get_light_state` reads that store and never
touches the network. Successful writes update the store immediately, so a read after a write
reflects the change without waiting for the next poll.

Distinguishing "never reached" from "was reachable, is not now" needs the timestamp to be
nullable — `None` means never confirmed.

*Alternatives:* read-through cache with a TTL (still blocks for a full timeout whenever the TTL
expires against a dead bulb); direct read per call (blocks every time); `pywizlight`'s
`start_push` (a powered-off bulb pushes nothing, so it cannot detect the case that matters).

### Serialise device access with a lock

The poll task and any number of concurrent tool calls share one `wizlight` instance over one UDP
socket. An `asyncio.Lock` around device access prevents interleaved request/response pairs and
prevents a poll landing between a write and its cache update.

This is not premature: an agent calling `turn_on` while a poll is in flight is the normal case,
not an edge case.

### Capabilities are fetched once and cached

`get_bulbtype()` returns the real `Features(...)` flags and `KelvinRange(min, max)` — the spec
requires validating colour temperature against the device's own range, and rejecting features the
device lacks. This needs one successful device call, so it is fetched on the first successful poll
and cached for the process lifetime.

Consequence, specified deliberately: if the bulb has never been reachable, `set_color_temp` cannot
validate its input and fails with "capabilities not yet known" rather than guessing a range.

### Writes go through `turn_on`, not `set_state`

Every write — switching on, and applying brightness, colour, colour temperature or a scene —
is sent with `wizlight.turn_on(PilotBuilder(...))`, which emits the protocol's `setPilot`.

`set_state()` looks like the better fit, since its docstring says it "doesn't turn on the
light", which would allow changing appearance without switching the bulb on. It does not work:
it emits a `setState` message that WiZ bulbs do not implement. pywizlight's own device fixtures
raise `No handler for setState`, which is strong evidence real firmware rejects it too.

**Consequence, accepted deliberately:** `set_brightness`, `set_color`, `set_color_temp` and
`set_scene` also switch the light on. There is no supported way to change a bulb's appearance
without doing so, and it is the behaviour an agent asking for an appearance most likely wants
anyway. It is documented in the README so it does not surprise anyone.

*Alternative:* hand-building a `setPilot` message without the `state` field, to set appearance
while leaving power alone — rejected as reimplementing the message layer this design chose not
to own, for a case no requirement asks for.

(Added after implementation: the first version used `set_state` and every setter failed.)

### Agent-facing units and names differ from the wire

| Agent-facing | Wire | Conversion |
| --- | --- | --- |
| brightness 0–100 % | `dimming` 0–255 | `round(pct * 255 / 100)`, and back |
| scene name, case-insensitive | `sceneId` int | name→id map from the library's scene table |
| Kelvin | `colortemp` | passthrough, validated against reported range |

Brightness is lossy in both directions — the device stores a coarser scale, so writing 200/255
reads back 199/255 (observed against the reference fake bulb). The specs therefore permit
read-back drift instead of treating it as failure. Percent is still the right interface: an agent
reasons in percentages, and 0–255 invites values that mean nothing to it.

Scene names are matched by case-folding, and an unknown name returns the valid list, so the agent
self-corrects within one call instead of needing `list_scenes` first.

### Input is validated before capabilities are consulted

Within a tool, checks run in this order:

1. **Device-independent validation** — is the value a whole number in range? Is the scene name a
   WiZ scene at all? Neither question needs the bulb.
2. **Capability checks** — does *this* bulb support colour? Is this Kelvin value inside the
   range it reports? Is this scene in its supported list? All require a successful device read.
3. **The device call.**

The order matters because it decides what an agent is told when the bulb has never been
reachable. With capabilities checked first, `set_brightness(150)` returns
`capabilities_unknown` — which is true but useless, since the value is invalid at any bulb and
would still be invalid once the bulb came back. Validating ranges first returns
`validation_error` naming the accepted range, which the agent can act on immediately.

Colour temperature is the deliberate exception: its valid range is a property of the specific
bulb, so with no capabilities there is genuinely nothing to validate against, and
`capabilities_unknown` is the honest answer. The specs carry that as an explicit scenario.

Scene names get both stages: the name is matched against the full WiZ scene table first, so a
typo is a validation error without a bulb, then narrowed to the bulb's supported list once
capabilities are known — which is also what lets the error name the available scenes.

(Added after implementation: the first version checked features first, so bad input against an
unreachable bulb reported the wrong error.)

### Error mapping

| `pywizlight` exception | Reported as |
| --- | --- |
| `WizLightTimeOutError` | bulb did not respond within the timeout |
| `WizLightConnectionError` | bulb unreachable on the network |
| `WizLightNotKnownBulb` | unrecognised device model |
| `WizLightMethodNotFound` | device rejected an unsupported operation |
| `WizLightError` | generic device error (catch-all, ordered last) |
| any other exception | internal error, logged, server stays up |

Every message carries the configured bulb address, so a wrong IP in `.env` is visible in the
error rather than presenting as a dead bulb. Ordering matters: `WizLightError` is the base class,
so it must be caught last.

Validation errors (out-of-range brightness, unknown scene) are raised before any device call, as
the specs require.

### Configuration and layout

A single settings object loads and validates `.env` at startup via `python-dotenv`, with
`os.environ` taking precedence. Missing `WIZ_BULB_IP` and malformed numbers fail startup with a
message naming the variable — the one case where failing fast is right, since it is a
configuration error, not an absent bulb.

```
  porch_agent/
    __init__.py
    config.py      settings + .env loading and validation
    bulb.py        wizlight wrapper: lock, poll loop, cache, error mapping
    tools.py       the eight MCP tools, annotations, unit conversion
    server.py      MCPServer construction, lifespan, run(streamable-http)
  harness.py       standalone MCP client, driven by hand against real hardware
  .env.example
  .gitignore
  pyproject.toml
```

`harness.py` sits outside the package: it is an operator tool, not part of the server, and
keeping it out enforces the spec's rule that it must not import server internals.

### Binding and host allow-listing

Defaults bind loopback. Because neither of the SDK's implicit behaviours is usable (see
Context), the server always constructs transport settings explicitly: when `MCP_ALLOWED_HOSTS`
is empty it allow-lists loopback — `127.0.0.1` and `localhost`, bare and with the configured
port — rather than passing nothing or an empty list. Protection therefore stays on, local
clients work without configuration, and reaching the server from another machine requires
setting `MCP_ALLOWED_HOSTS`.

When `MCP_HOST` is non-loopback and `MCP_ALLOWED_HOSTS` is empty, the server logs a warning
naming the variable at startup. Silence here is the failure mode worth designing against: the
server appears healthy while rejecting every remote request with `421`, which reads as a broken
server rather than a configuration gap.

Origins are not allow-listed. Non-browser clients send no `Origin` header, which the middleware
permits; a browser-based client would need origins added.

## Risks / Trade-offs

- **Nothing is validated before the operator pulls.** → The harness reports per-tool pass/fail
  with the error text, so one hardware run localises any fault. Startup and import are verifiable
  locally even without a bulb.
- **The README/reality gap invites regressions.** A later contributor reading `pywizlight`'s docs
  will reintroduce port 12345 or `bulb.state.get_state()`. → The corrections are recorded in this
  document and belong in code comments at each site.
- **No automated tests at all.** → Accepted for v1. `pywizlight.tests.fake_bulb` ships in the
  wheel and works, so a hardware-free suite is cheap to add later; the layout above keeps
  `bulb.py` injectable with an address and port to make that straightforward.
- **Unauthenticated HTTP controlling physical hardware.** → Loopback by default, and a deliberate
  non-goal. `MCPServer` accepts `auth`/`token_verifier` if that changes.
- **Python 3.11 with zero headroom** against `pywizlight`'s floor. → Declare `requires-python
  >=3.11` and check the target machine's interpreter before deploying.
- **Brightness round-trips are lossy.** → Specified as acceptable drift rather than treated as a
  bug, so it is not "fixed" later by exposing raw 0–255.
- **A stalled poll task would freeze reachability reporting** while the server still looks
  healthy. → Every device call is timeout-bounded, and the loop catches all exceptions so a
  failure cannot end it.

## Migration Plan

No migration: nothing exists yet, and there is no data or prior deployment. Deployment is
`git pull` on the machine that can reach the bulb, install dependencies, write `.env` with the
bulb's address, start the server, then run the harness against the bulb. Rollback is reverting to
the previous commit; the server holds no persistent state.

## Open Questions

None that affect the specs, the approach, or the task breakdown.
