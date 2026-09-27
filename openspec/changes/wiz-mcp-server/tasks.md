# Tasks

## 1. Project scaffolding

- [x] 1.1 Create `.gitignore` covering `__pycache__/`, `*.pyc`, `.venv/`, `venv/`, and `.env`, and verify `git status --short` stays clean after creating a throwaway `.env` and running Python once
- [x] 1.2 Create `pyproject.toml` declaring `requires-python = ">=3.11"` and dependencies `pywizlight`, `mcp>=2`, `python-dotenv` (note: `python-dotenv` is only an extra of `mcp`, so it must be declared explicitly; `uvicorn` and `starlette` arrive via `mcp`), and verify a clean install of the project succeeds
- [x] 1.3 Create the `porch_agent/` package with empty `config.py`, `bulb.py`, `tools.py`, `server.py` per design.md's layout, and verify `python -c "import porch_agent"` succeeds
- [x] 1.4 Create `.env.example` with all eight variables from the server-runtime spec, each commented with its default, and verify every variable named in that spec appears

## 2. Configuration

- [x] 2.1 Implement a settings loader in `config.py` that loads `.env` via `python-dotenv` with `os.environ` taking precedence, and verify a value set in the environment overrides the same key in `.env`
- [x] 2.2 Apply defaults for every variable except `WIZ_BULB_IP` (bulb port 38899 — pywizlight's real default, not the 12345 in its README), and verify the server's settings load from a `.env` containing only `WIZ_BULB_IP`
- [x] 2.3 Fail startup with a message naming the missing variable when `WIZ_BULB_IP` is absent, and verify the message names both the variable and the file it is expected in
- [x] 2.4 Fail startup with a message naming the variable and its value when a numeric setting is not a valid positive number, and verify with a deliberately malformed poll interval

## 3. Bulb connection layer

- [x] 3.1 Implement a `bulb.py` wrapper holding one `wizlight` instance constructed from settings, taking address and port as arguments so it stays injectable, and verify it constructs without any network access
- [x] 3.2 Add an `asyncio.Lock` serialising all device access, and verify a concurrent read and write do not interleave
- [x] 3.3 Implement the state cache: last known state, a nullable last-confirmed timestamp, and a reachability flag, with `None` meaning never confirmed, and verify a fresh instance reports not reachable and never confirmed
- [x] 3.4 Implement the poll loop reading state on `WIZ_POLL_INTERVAL_SECONDS`, updating the cache, and verify it survives consecutive failures without exiting by pointing it at an address with no bulb
- [x] 3.5 Bound every device call by `WIZ_REQUEST_TIMEOUT_SECONDS`, and verify a call against an unreachable address returns in approximately that time rather than hanging
- [x] 3.6 Map the five `pywizlight` exceptions to distinct structured errors per design.md's table, catching `WizLightError` last since it is the base class, and include the configured bulb address in every message; verify by unit-triggering each mapping
- [x] 3.7 Fetch `get_bulbtype()` on first successful poll and cache `Features` and `KelvinRange` for the process lifetime, and verify a second read performs no additional device call
- [x] 3.8 Update the cache immediately after a successful write, and verify a state read straight after a write reflects the change without waiting for a poll
- [x] 3.9 Log reachability transitions in both directions, and verify the log shows a transition when a poll starts failing

## 4. MCP tools

- [x] 4.1 Implement `get_light_state` reading only from cache, reporting reachability and last-confirmed time, and verify it returns without any device call while the bulb is unreachable
- [x] 4.2 Implement `turn_on` and `turn_off`, with `turn_on` optionally accepting brightness, colour, or colour temperature applied in the same call, and verify both are idempotent against an already-matching state
- [x] 4.3 Implement `set_brightness` accepting 0–100 percent with `round(pct * 255 / 100)` conversion both ways, and verify an out-of-range value is rejected naming the range with no device call made
- [x] 4.4 Implement `set_color` validating each RGB component to 0–255, and verify an out-of-range component is rejected before any device call
- [x] 4.5 Implement `set_color_temp` validating Kelvin against the bulb's reported `KelvinRange`, and verify it fails with "capabilities not yet known" when the bulb has never been reachable
- [x] 4.6 Implement `set_scene` matching names case-insensitively via a name→id map, returning the valid names on an unknown input, and verify "cozy", "Cozy" and "COZY" select the same scene
- [x] 4.7 Implement `list_scenes` returning supported scene names, and verify it requires no device call once capabilities are cached
- [x] 4.8 Reject operations the bulb's `Features` flags say it does not support, with an error stating so, and verify against a feature flag set to false
- [x] 4.9 Set `ToolAnnotations` — read-only on `get_light_state` and `list_scenes`, idempotent and not read-only on the six mutating tools — and verify the annotations appear in a listed tool schema

## 5. Server assembly

- [x] 5.1 Construct `MCPServer` in `server.py` using `from mcp.server.mcpserver import MCPServer` (not `FastMCP`, which no longer exists in mcp 2.x), register all eight tools, and verify `list_tools` over a live session returns exactly those eight with no bulb parameter on any schema
- [x] 5.2 Implement the `lifespan` context manager creating the bulb wrapper and starting the poll task on entry, cancelling the task and closing the connection on exit, and verify the process exits promptly on SIGINT without hanging
- [x] 5.3 Serve over streamable-HTTP on the configured host, port, and path, and verify an MCP client connects and calls a tool over HTTP
- [x] 5.4 Warn at startup, naming `MCP_ALLOWED_HOSTS`, when `MCP_HOST` is non-loopback and no hosts are allow-listed, and verify the warning appears with a non-loopback bind and absent allow-list
- [x] 5.5 Log effective configuration at startup — bound address and path, bulb address, poll interval — and verify all four appear in the startup output
- [x] 5.6 Add a console entry point to start the server, and verify it launches with the bulb powered off and still serves its tool list

## 6. Test harness

- [x] 6.1 Create `harness.py` outside the package, connecting to a running server over streamable-HTTP with the address taken from configuration or a command-line argument, and verify it runs against a non-default host and port
- [x] 6.2 Verify the advertised tool list matches the expected eight and report any missing tool rather than skipping it, and verify by removing a tool registration temporarily
- [x] 6.3 Capture the light's state at start and restore it on exit including after a failure, and verify restoration happens on an exception path
- [x] 6.4 Call every tool in sequence with a brief pause between visible changes, reporting per-tool pass/fail with the returned value or error text, and verify all eight appear in the report
- [x] 6.5 Exit non-zero if any check failed and zero when all pass, and verify both exit codes
- [x] 6.6 Report an unreachable server distinctly from a server reporting an unreachable bulb, naming where it tried to connect, and verify by running with no server listening

## 7. Documentation

- [x] 7.1 Write a README covering install, `.env` setup, starting the server, and running the harness, written for the target machine rather than the development environment, and verify by following the steps from a clean checkout
- [x] 7.2 Add code comments at each site where `pywizlight`'s README contradicts its shipped behaviour — port 38899, `updateState()` returning a list indexed at `[0]`, `PilotBuilder(colortemp=...)`, `getMac()` — and verify each of the four is annotated

## 8. Local verification without hardware

- [x] 8.1 Start the server with `WIZ_BULB_IP` pointing at an address with no bulb, and verify it starts, serves eight tools, and `get_light_state` reports not reachable and never confirmed
- [x] 8.2 Confirm every validation error path fires with no device present — out-of-range brightness, out-of-range RGB, unknown scene name — and verify each returns a validation error rather than a timeout

## 9. Hardware validation (operator, on the target machine)

- [x] 9.1 Confirm the target machine's Python is 3.11 or newer before deploying, since `pywizlight` requires it and the development environment sits exactly at that floor
- [x] 9.2 Pull the repository, install, write `.env` with the real bulb address, start the server, and verify the startup log shows the expected configuration
- [x] 9.3 Run the harness against the real bulb and verify every tool passes, each change is visible on the light, and the original state is restored
- [x] 9.4 Power the bulb off, and verify `get_light_state` reports it unreachable promptly and the server keeps running; power it back on and verify reachability returns without a restart
