# Porch Agent

**An MCP server wrapped around a WiZ smart lightbulb, so that an AI agent can control it.**

## What this is

A [WiZ](https://www.wizconnected.com/) smart bulb is controllable only by sending messages to
it over an undocumented UDP protocol on your local network. That is fine for a script, but an
AI agent cannot use it: there is nothing for the agent to call.

This project closes that gap. It is a server speaking the
[Model Context Protocol](https://modelcontextprotocol.io) (MCP) — the standard way to give an
agent a set of tools it can call. The server turns the bulb's UDP protocol into eight plain
tools (`turn_on`, `set_brightness`, `set_scene`, and so on). Point an agent at it, and the
bulb becomes something the agent can see the state of and operate, in the same way it uses any
other tool.

```
   +-----------+   MCP over HTTP    +--------------+   UDP :38899   +--------+
   |   Agent   | -----------------> | Porch Agent  | -------------> |  WiZ   |
   |           |   "turn_on",       |  MCP server  |   setPilot,    |  bulb  |
   |           |   "set_scene"      |              |   getPilot     |        |
   +-----------+ <----------------- +--------------+ <------------- +--------+
                    light state          ^
                                         | polls every 30s so it always
                                         | knows whether the bulb is there
```

So instead of writing code to talk to the bulb, you can ask an agent to "dim the porch light
to 20%" or "set the porch to Cozy", and it calls the right tool with the right arguments. The
tools are described to the agent in units it gets right first time: brightness as a
**percentage**, scenes by **name** rather than a numeric id, and colour temperature validated
against the range your particular bulb actually supports.

The transport is streamable-HTTP rather than stdio, which means the agent does not have to run
on the same machine as the bulb. The server runs wherever it can reach the bulb; the agent
connects to it over the network.

### One deliberate design choice

The bulb is assumed to be one you may switch off at the wall. The server therefore treats
"unreachable" as a normal state rather than an error: it always starts even with the bulb dead,
polls for it in the background, and answers state queries instantly from a cache. A naive
wrapper would stall every call for seconds waiting on a bulb that isn't there.

## What you get

- **The server** (`porch-agent`) — the thing your agent talks to. See
  [Run the server](#run-the-server).
- **The test harness** (`harness.py`) — a standalone script that drives a running server
  against your real bulb and reports pass/fail per tool, so you can confirm the whole path
  works. See [Run the test harness](#run-the-test-harness).
- **The porch light agent** (`agent/`, `porch-light`) — watches your phone's location and,
  as you approach home, has an AI agent set the porch light for the conditions. See
  [Location-triggered porch light agent](#location-triggered-porch-light-agent).

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                  # install
cp .env.example .env              # then set WIZ_BULB_IP to your bulb's address
porch-agent                       # start the server on http://127.0.0.1:8000/mcp
python harness.py                 # in another terminal: check it against the real bulb
```

## Requirements

- **Python 3.11 or newer.** `pywizlight` requires it. Check before deploying:
  ```bash
  python3 --version
  ```
- A WiZ bulb on the same network as this server, and its IP address.

## Install

```bash
git clone https://github.com/steveh250/Porch-Agent.git
cd Porch-Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

Copy the example and set your bulb's address. That is the only required setting:

```bash
cp .env.example .env
$EDITOR .env          # set WIZ_BULB_IP
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `WIZ_BULB_IP` | *(required)* | Address of the bulb |
| `WIZ_BULB_PORT` | `38899` | Bulb's UDP port |
| `WIZ_POLL_INTERVAL_SECONDS` | `30` | How often to check the bulb |
| `WIZ_REQUEST_TIMEOUT_SECONDS` | `5` | Timeout for one bulb request |
| `MCP_HOST` | `127.0.0.1` | Address to bind |
| `MCP_PORT` | `8000` | Port to bind |
| `MCP_PATH` | `/mcp` | Path the endpoint is served on |
| `MCP_ALLOWED_HOSTS` | *(empty)* | Host values accepted from clients |

Variables already set in the environment take precedence over `.env`, so a deployment can
override any of them without editing the file.

A missing `WIZ_BULB_IP`, or a setting that is not a positive number, stops startup with a
message naming the variable. An unreachable *bulb* does not — that is expected.

## Run the server

```bash
porch-agent
```

It logs the configuration actually in effect, so you can confirm it at a glance:

```
INFO  porch_agent: Serving MCP over streamable-HTTP on http://127.0.0.1:8000/mcp
INFO  porch_agent: Bulb address: 192.168.1.42:38899
INFO  porch_agent: Poll interval: 30s
INFO  porch_agent: Request timeout: 5s
INFO  porch_agent.bulb: Bulb at 192.168.1.42:38899 is now reachable
```

### Connecting an agent

Point your agent at the endpoint the server logged — by default
`http://127.0.0.1:8000/mcp`. Most MCP clients take a URL for an HTTP server; in a JSON config
that usually looks like:

```json
{
  "mcpServers": {
    "porch-agent": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Once connected, the agent discovers the eight tools by itself. You can then ask it things like
"what is the porch light doing?", "dim the porch to 20%", or "set the porch light to Cozy" and
it will pick the matching tool. The server also tells the agent, in its instructions, that the
bulb may be powered off and that this is expected rather than a fault.

### Reaching it from another machine

Set `MCP_HOST=0.0.0.0` **and** `MCP_ALLOWED_HOSTS`. Both are needed: Host-header protection
is active, so with no allow-list only loopback clients are accepted and remote requests are
rejected with `421 Misdirected Request`. List the host values clients connect as:

```
MCP_HOST=0.0.0.0
MCP_ALLOWED_HOSTS=porch.local:8000,192.168.1.10:8000
```

The server warns at startup if you bind off-loopback without an allow-list.

There is no authentication. Run it only on a network you trust.

## Run the test harness

The harness is a standalone MCP client that drives a **running server** against your **real
bulb**, so you can confirm the whole path works on the machine that can actually reach it.
It changes the light visibly, then puts it back how it found it.

In one terminal:

```bash
porch-agent
```

In another:

```bash
python harness.py                 # uses .env for the server address
python harness.py --pause 3       # linger 3s on each change, easier to watch
python harness.py --url http://192.168.1.10:8000/mcp
python harness.py --scene-sample 10   # cycle through 10 random scenes, not 5
python harness.py --seed 12345        # repeat a previous run's scene sample
```

It prints a pass/fail line per tool and exits non-zero if anything failed, so it is usable
from a script. It distinguishes the two failures worth telling apart:

| Exit code | Meaning |
| --- | --- |
| `0` | Every check passed |
| `1` | One or more checks failed |
| `2` | Could not reach the **server** — is it running? |
| `3` | Server is fine, but it reports the **bulb** as unreachable |

### What it checks

1. **The tool list** — that all eight tools are advertised, naming any that are missing rather
   than skipping them, and that there are no unexpected extras.
2. **Reachability** — that the server reports your bulb as reachable before it tries anything
   else. If not, it stops there and says so.
3. **Every tool, in sequence**, pausing between each so you can watch the light: on, full
   brightness, dim, colour red, colour blue, warm white, then a **random sample of five
   scenes** drawn from whatever your bulb advertises, and one of them again in lower case to
   prove name matching ignores case. The sample is seeded and the seed is printed, so a run
   that fails can be repeated exactly with `--seed`. `Custom Mode 1-10` are excluded, since
   those slots are empty unless you have filled them in the WiZ app.
4. **Rejections** — that invalid input is refused rather than sent to the bulb: brightness of
   150, a colour component of 300, a scene that does not exist. These are expected to fail, and
   the harness fails if any of them *succeeds*.

### It puts your light back

The harness records the light's state before it starts and restores it when it finishes —
including when a check fails partway through. Running it does not leave your porch light in an
unexpected state.

### Example output

```
Connecting to http://127.0.0.1:8000/mcp

Tool list
  [PASS] tool advertised: get_light_state
  [PASS] tool advertised: turn_on
  ...
  [PASS] no unexpected tools

Bulb reachability
  [PASS] get_light_state -- on=False bright=13%
  [PASS] bulb reachable

  Recorded original state: {'on': False, 'brightness_pct': 13, ...}

Visible changes (watch the light)
  [PASS] turn_on -- on=True bright=13%
  [PASS] set_brightness 100% -- on=True bright=100%
  [PASS] set_scene 'Cozy' -- on=True bright=20%
  ...

Rejections (these SHOULD fail)
  [PASS] set_brightness 150 rejected -- [validation_error] brightness_pct must be between 0 and 100, got 150.
  ...

Restoring original state via turn_off({})
  [PASS] original state restored

==============================================================
  26 passed, 0 failed, 26 checks total
==============================================================
```

## Tools

| Tool | Notes |
| --- | --- |
| `get_light_state` | Read-only. Served from the poll cache, so it never blocks. Reports `reachable`, `ever_confirmed`, `last_confirmed` and `kelvin_range` (`{min, max}`, or null until the bulb has been reached). |
| `turn_on` | Optionally takes `brightness_pct`, `red`/`green`/`blue`, or `color_temp_kelvin`. |
| `turn_off` | |
| `set_brightness` | `brightness_pct` is **0–100**, not the device's 0–255. |
| `set_color` | `red`, `green`, `blue`, each 0–255. |
| `set_color_temp` | `color_temp_kelvin`, validated against the range your bulb reports. |
| `set_scene` | By **name** (`"Cozy"`), case-insensitive — not a numeric scene id. |
| `list_scenes` | Read-only. The scene names your bulb supports. |

Setting an appearance also switches the light on; there is no supported way to change a WiZ
bulb's appearance without doing so.

Brightness round-trips are slightly lossy: the device stores a coarser scale, so a value read
back may differ by a point or two from the one written. That is expected.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `421 Misdirected Request` | `MCP_ALLOWED_HOSTS` does not list the host the client is connecting as. |
| `Configuration error: WIZ_BULB_IP is not set` | No `.env`, or the variable is missing from it. |
| `[bulb_unreachable]` / `reachable: false` | Bulb is powered off, or `WIZ_BULB_IP` is wrong. The address in the error is the one being used. |
| `[capabilities_unknown]` | The bulb has not been reachable yet, so its Kelvin range and scene list are unknown. |
| `[bulb_rejected]` — reads work, but every write is refused | Recent WiZ firmware requires signed commands, which pywizlight cannot produce. Turn off message signing for the bulb in the WiZ app; writes then work normally. |
| `[bulb_unsupported_operation]` | Your bulb model does not have that feature (e.g. colour on a white-only bulb). |

## Location-triggered porch light agent

The [`agent/`](agent/) directory holds a second, separate program. When your phone gets close to
home, an AI agent decides how to set the porch light (on or off, brightness, colour temperature)
from the time of day, how dark it is and the weather. It then does it through the MCP server
above.

```
 OwnTracks --MQTT--> Mosquitto --> location monitor ----arrival----> decision agent --MCP/HTTP--> porch-agent --> bulb
 (phone)                            (no LLM: distance,                (Agent Framework,              server
                                     hysteresis, once                  OpenRouter LLM,
                                     per arrival)                      sun + weather tools)
                                                 \___ deterministic guardrails: 30 s timeout, fallback,
                                                      clamping, verification, JSONL log ___/
```

**Location monitor (deterministic, no LLM).**
- Subscribes to OwnTracks on a local Mosquitto broker and ignores fixes less accurate than 100 m.
- Measures the distance to home and runs a state machine: `AWAY → APPROACHING` below 400 m,
  which fires the agent once, then `NEAR` below 200 m.
- Re-arms only after you go back beyond 400 m. GPS wobble near home never fires the agent a
  second time.
- The first fix after startup never fires either: restarting the monitor while you are at home is
  not an arrival.
- A fix more than 2 minutes old (`presence.max_fix_age_s`, judged by its OwnTracks timestamp) can
  never fire an arrival. When the phone has been offline, OwnTracks sends its queued fixes on
  reconnecting. The state machine follows that backlog without acting on it, so reaching the
  door with a backlog does not switch the light on late.

**Decision agent.** Built with [Microsoft Agent Framework](https://github.com/microsoft/agent-framework):
- It talks to an OpenRouter model through the OpenAI-compatible Chat Completions client.
- Its tools are `get_sun_context` (computed locally with astral), `get_weather` (OpenWeatherMap,
  cached for 10 minutes) and the server's `get_light_state` / `turn_on` / `turn_off`.
- Its policy is plain prose in [`agent/policy.md`](agent/policy.md). Edit it freely.
- It must set the light, read the state back to check it, retry once on a mismatch, and finish
  with a one-sentence reason.

**Resting light (deterministic).** When nobody is arriving, the light rests at a dim
20 % / 2700 K from civil dusk to civil dawn and is off in daylight. An arrival hands it to the
agent, and 10 minutes later it goes back to resting. It only acts at dusk, at dawn, after an
arrival and at startup, so a setting you make by hand is left alone until the next of those.
Configure it (or set `enabled: false`) in the `resting:` section of `config.yaml`.

**Guardrails (deterministic, not negotiable).**
- The agent run has a hard 30 s timeout.
- If the LLM fails, times out, or weather is unavailable, the fallback calls the server directly.
  After civil dusk it switches the light on at 80 % / 2700 K; in daylight it does nothing.
  An API outage never leaves you in the dark.
- Every bulb call the agent makes is clamped first: brightness to 10–100 %, colour temperature to
  the range the bulb reports. RGB and scenes are not available to the agent.
- After every arrival, the bulb's actual state is read back and checked against the decision,
  with one re-apply on a mismatch.
- Each arrival appends one line to `agent/logs/arrivals.jsonl`. The line records the timestamp,
  distance, situation (sun and weather), decision, reason, tool calls, verification result and
  whether the fallback was used.

### Why it has its own virtualenv

Agent Framework's MCP client is written against the 1.x `mcp` SDK. The server needs 2.x, and the
two cannot be installed together. So `agent/` is its own Python project with its own venv, and it
reaches the server over streamable-HTTP like any other client, where the SDK version does not
matter. It depends on `agent-framework-core` and `agent-framework-openai` rather than the
`agent-framework` umbrella package, which pulls in about 30 integrations and pins `mcp<2`.

### Running it under WSL

For the complete build, from bulb to phone, see **[docs/HOWTO.md](docs/HOWTO.md)**. The steps
below are the short version for the two programs.

These steps assume Ubuntu on WSL2, with the MCP server set up as described above. Mosquitto is
assumed to be running on the same machine, on `localhost:1883`.

**1. Start the server** (terminal 1, from the repo root, in the server's venv):

```bash
porch-agent
```

**2. Install the agent** (from `agent/`, in a separate venv):

```bash
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml   # edit in place: home, presence radii (800/400), mqtt.username
cp .env.example .env                 # set OPENROUTER_API_KEY, OWM_API_KEY, MQTT_PASSWORD
```

**3. Check it offline:**

```bash
pytest
porch-light simulate sim/arrival.jsonl --fake-light --force-fallback --at 2026-11-20T21:30
```

The sample files in `sim/` are laid out around the example config's home coordinates, so they
stop firing once `config.yaml` has your own. HOWTO Step 6 has a short command that generates a
walk home (`sim/home.jsonl`) for your coordinates.

**4. Test a full arrival without leaving the house.**
- This command uses the real LLM, weather and server. `--at` pretends it is that time, in your
  home timezone:

  ```bash
  porch-light simulate sim/home.jsonl --at 2026-11-20T19:30
  ```

- The `simulate` options are `--fake-light` (in-memory bulb, no server needed),
  `--force-fallback` (no LLM or weather), `--at` and `--delay`.
- A simulation file has one OwnTracks JSON payload per line. `#` starts a comment, and a bare
  `{"lat":…,"lon":…,"acc":…}` line is treated as a location.

**5. Run the monitor** (terminal 2):

```bash
porch-light run
```

It runs until you stop it and reconnects to the broker on its own. `porch-light -v run` adds
debug detail; HOWTO Step 6 ("Run it by hand") shows what normal output looks like. To keep it running when no
terminal is open, run both `porch-agent` and `porch-light run` under systemd in WSL
(`systemd=true` in `/etc/wsl.conf`) or in `tmux`.

### OwnTracks settings

In the OwnTracks app on the phone:

- **Mode:** MQTT (not HTTP).
- **Host / port:** your broker, 1883 (or whatever your broker listens on).
- **Topic:** the default, `owntracks/<username>/<device>`. The monitor subscribes to
  `owntracks/+/+` by default. Set `mqtt.topic` in `config.yaml` to `owntracks/<you>/<phone>` to
  listen to one phone only.
- **Monitoring mode:** leave it on *Significant*. It reports roughly every 500 m of movement,
  so set `presence.outer_radius_m` to about 800 (and `inner_radius_m` to 400) so that at least
  one report lands inside the trigger distance on the way home. See
  [docs/HOWTO.md](docs/HOWTO.md), Step 7. *Move* mode reports more often but drains the battery.
- **Identification:** if the broker requires a username, set `mqtt.username` in `config.yaml`
  and `MQTT_PASSWORD` in `.env`.

Getting the phone to reach the broker from outside the house (Tailscale, WSL mirrored
networking, broker listeners and authentication), plus running everything as services, is
covered step by step in [docs/HOWTO.md](docs/HOWTO.md).

### Tuning

- **`agent/policy.md`** is the agent's system prompt. Change the rules in plain English. A fixed
  operating protocol (set, verify, retry once, report JSON) is appended in code, so edits to the
  policy cannot break the verification or logging.
- **`agent/config.yaml`** holds the radii, accuracy threshold, maximum fix age, model id (any OpenRouter model
  with tool calling), timeouts, fallback setting, Kelvin limits (the narrower of these and the
  bulb's own range wins), the resting light and file locations. Edit each setting in place: a
  key that appears twice in the same section is rejected with an error naming both lines.
- **Updating:** `git switch main && git pull`, then restart both programs. `git pull` never
  touches your `config.yaml` or `.env`; new settings appear in `config.example.yaml` and use
  their defaults until you add them.
- **Arrivals vs. leaving:** only arrivals run the agent, and it sets the light for the
  conditions whatever state it was in. Leaving does nothing to the light. See HOWTO
  "Customising the behaviour" for a policy rule that leaves a light you've set yourself alone.

## Acknowledgements

This project is a thin layer over other people's work, and would not exist without it:

- **[pywizlight](https://github.com/sbidy/pywizlight)** by [Stephan Traub](https://github.com/sbidy)
  and contributors (MIT) — does the genuinely hard part: speaking WiZ's undocumented UDP
  protocol, across firmware revisions and device models, including the per-model capability
  tables and scene mappings this server relies on. Everything here sits on top of it.
- **[Model Context Protocol](https://modelcontextprotocol.io)** and the
  [Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT) — the tool protocol
  and the server implementation.
- **[uvicorn](https://www.uvicorn.org/)** and **[Starlette](https://www.starlette.io/)**
  (BSD-3-Clause) — the HTTP serving underneath, arriving as dependencies of the MCP SDK.
- **[python-dotenv](https://github.com/theskumar/python-dotenv)** (BSD-3-Clause) —
  `.env` loading.

These are ordinary dependencies, fetched at install time rather than copied into this
repository, so each ships its own license alongside it.

## License

[MIT](LICENSE). Use it as you see fit.

## Notes for maintainers

**`pywizlight`'s README disagrees with its shipped code.** Verified against 0.6.6 — follow the
code, not the docs:

- The default port is **38899**. The README says `12345`.
- `updateState()` returns `List[Optional[PilotParser]]`, so state is at index `[0]`. The
  README's `bulb.state.get_state()` raises `AttributeError`.
- `PilotBuilder` takes `colortemp=`, not `kelvin=`.
- The method is `getMac()`, not `getMAC()`. (Not used here, but easy to trip over.)
- `set_state()` emits a `setState` message that bulbs do not implement; `turn_on()` emits the
  supported `setPilot`.

**`mcp` 2.x renamed `FastMCP` to `MCPServer`** (`mcp.server.mcpserver`), and its models use
snake_case (`input_schema`, `is_error`, `read_only_hint`). Most MCP examples online are 1.x
`FastMCP` code and will not run.

There are no automated tests; validation is by hand with the harness against real hardware.
`pywizlight` does ship a working fake bulb at `pywizlight.tests.fake_bulb.startup_bulb()` if
a hardware-free suite is ever wanted.
