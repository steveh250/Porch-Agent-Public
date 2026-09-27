# Spec Delta

## Purpose

Defines how the server is configured and exposed on the network, so an agent on the same host
or a different one can reach it and an operator can deploy it by editing a single file.

## ADDED Requirements

### Requirement: Configuration comes from a .env file

The server SHALL read its configuration from environment variables, loaded from a `.env` file
in the project root when present. The following SHALL be supported:

| Variable | Meaning |
| --- | --- |
| `WIZ_BULB_IP` | Address of the bulb to control |
| `WIZ_BULB_PORT` | UDP port of the bulb |
| `WIZ_POLL_INTERVAL_SECONDS` | Interval between presence polls |
| `WIZ_REQUEST_TIMEOUT_SECONDS` | Timeout for a single device request |
| `MCP_HOST` | Address the server binds |
| `MCP_PORT` | Port the server binds |
| `MCP_PATH` | HTTP path the MCP endpoint is served on |
| `MCP_ALLOWED_HOSTS` | Host values accepted in requests |

Every variable except `WIZ_BULB_IP` SHALL have a working default, so a minimal `.env` needs
only the bulb address.

An environment variable already set in the environment SHALL take precedence over the `.env`
file, so a deployment can override configuration without editing the file.

#### Scenario: Minimal configuration

- **WHEN** the server starts with a `.env` containing only `WIZ_BULB_IP`
- **THEN** the server starts using defaults for every other setting

#### Scenario: Bulb address is missing

- **WHEN** the server starts with no bulb address configured
- **THEN** startup fails with a message naming the missing variable
- **AND** the message states which file it is expected in

#### Scenario: Malformed numeric setting

- **WHEN** a numeric setting such as the poll interval is not a valid positive number
- **THEN** startup fails with a message naming the offending variable and its value

#### Scenario: Environment overrides the file

- **WHEN** a variable is set both in the environment and in `.env`
- **THEN** the value from the environment is used

### Requirement: Served over streamable-HTTP

The server SHALL expose its MCP endpoint over streamable-HTTP at the configured host, port,
and path, so that the agent and the server may run on different machines.

#### Scenario: Agent connects over HTTP

- **WHEN** an MCP client connects to the configured host, port, and path
- **THEN** the MCP session is established and tools can be listed and called

#### Scenario: Default binding is loopback

- **WHEN** no bind address is configured
- **THEN** the server binds loopback only, so it is not exposed on the network by default

### Requirement: Non-loopback binding requires host allow-listing

Because requests carrying an unexpected host value are rejected by default as a
cross-origin protection, the server SHALL allow the accepted host values to be configured.

When the server is configured to bind an address other than loopback and no host values have
been allow-listed, it SHALL warn at startup that remote requests will be rejected, rather than
leaving the operator to diagnose rejected requests as a malfunction.

#### Scenario: Remote binding without allow-listed hosts

- **WHEN** the server is configured to bind a non-loopback address and no host values are
  allow-listed
- **THEN** the server logs a warning at startup explaining that remote requests will be
  rejected and naming the variable that fixes it

#### Scenario: Remote agent reaches an allow-listed server

- **WHEN** the server binds a non-loopback address with the host value allow-listed
- **AND** an agent on another machine connects
- **THEN** the MCP session is established

### Requirement: Startup is observable

The server SHALL log, at startup, the address and path it is serving, the bulb address it is
configured for, and the poll interval, so an operator can confirm the configuration actually
in effect.

It SHALL log when the bulb's reachability changes, so a bulb powered on or off is visible in
the log.

#### Scenario: Startup log states effective configuration

- **WHEN** the server starts
- **THEN** it logs the bound address and path, the configured bulb address, and the poll
  interval

#### Scenario: Reachability transitions are logged

- **WHEN** the bulb becomes reachable or stops being reachable
- **THEN** the transition is logged

### Requirement: Runs on Python 3.11 or newer

The project SHALL declare a minimum Python version of 3.11, and SHALL fail with a clear
message rather than an obscure import error when run on an older interpreter.

#### Scenario: Supported interpreter

- **WHEN** the server is run on Python 3.11 or newer
- **THEN** it starts normally

#### Scenario: Unsupported interpreter

- **WHEN** installation or startup is attempted on an interpreter older than 3.11
- **THEN** it fails with a message naming the required version
