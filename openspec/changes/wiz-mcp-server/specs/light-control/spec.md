# Spec Delta

## Purpose

Defines the MCP tool surface an agent uses to observe and operate the single WiZ bulb,
presenting device capabilities in units and names an agent can use correctly without
knowing the underlying wire protocol.

## ADDED Requirements

### Requirement: Advertised tool surface

The server SHALL advertise exactly eight tools: `get_light_state`, `turn_on`, `turn_off`,
`set_brightness`, `set_color`, `set_color_temp`, `set_scene`, and `list_scenes`.

No tool SHALL accept a bulb identifier argument, because the server operates exactly one
bulb determined by its configuration.

#### Scenario: Agent lists available tools

- **WHEN** an MCP client requests the tool list
- **THEN** all eight tools are returned with descriptions and input schemas
- **AND** no tool schema contains a bulb, device, IP, or address parameter

#### Scenario: Read-only and idempotency hints are advertised

- **WHEN** an MCP client inspects tool annotations
- **THEN** `get_light_state` and `list_scenes` are annotated as read-only
- **AND** `turn_on`, `turn_off`, `set_brightness`, `set_color`, `set_color_temp`, and
  `set_scene` are annotated as idempotent and not read-only

### Requirement: Reading light state

`get_light_state` SHALL report the bulb's last known power state, brightness, colour, and
colour temperature, together with whether the bulb is currently reachable, when that
information was last confirmed, and the colour temperature range the bulb supports (null
until the bulb has been reached).

It SHALL return promptly and SHALL NOT wait on a device round trip, so that an unreachable
bulb does not delay the caller.

#### Scenario: Bulb is on and reachable

- **WHEN** `get_light_state` is called while the bulb is powered and reachable
- **THEN** the result reports the bulb as reachable
- **AND** includes power state, brightness as a percentage, and current colour or colour
  temperature
- **AND** includes the time the state was last confirmed

#### Scenario: Bulb is powered off

- **WHEN** `get_light_state` is called while the bulb is powered off at the wall
- **THEN** the result reports the bulb as not reachable
- **AND** reports when it was last successfully reached, or that it has never been reached
- **AND** returns without waiting for a device timeout

### Requirement: Switching the light on and off

`turn_on` SHALL switch the bulb on and `turn_off` SHALL switch it off. `turn_on` SHALL
optionally accept brightness, colour, or colour temperature to apply in the same call, so an
agent can reach a desired appearance without a second round trip.

Both SHALL be idempotent: invoking either against a bulb already in that state SHALL succeed.

#### Scenario: Turning the light on

- **WHEN** `turn_on` is called with no arguments and the bulb is reachable
- **THEN** the bulb is switched on
- **AND** the result reports the resulting power state

#### Scenario: Turning on with an appearance in one call

- **WHEN** `turn_on` is called with a brightness and a colour
- **THEN** the bulb is switched on with that brightness and colour applied

#### Scenario: Turning off a light already off

- **WHEN** `turn_off` is called and the bulb is already off
- **THEN** the call succeeds and reports the bulb as off

### Requirement: Brightness expressed as a percentage

`set_brightness` SHALL accept brightness as an integer percentage from 0 to 100, and all
reported brightness values SHALL likewise be percentages. The agent-facing interface SHALL
NOT expose the device's native brightness scale.

A value outside 0 to 100 SHALL be rejected as a validation error naming the accepted range,
without any request being sent to the bulb.

Because the device stores brightness on a coarser scale, a value read back after a write MAY
differ slightly from the value written; such rounding SHALL NOT be reported as a failure.

#### Scenario: Setting brightness to a valid percentage

- **WHEN** `set_brightness` is called with 50
- **THEN** the bulb's brightness is set to approximately half
- **AND** the result reports the resulting brightness as a percentage

#### Scenario: Rejecting an out-of-range percentage

- **WHEN** `set_brightness` is called with 150
- **THEN** the call fails with a validation error naming the accepted range of 0 to 100
- **AND** no request is sent to the bulb

#### Scenario: Device rounding on read-back

- **WHEN** a brightness is written and then read back
- **THEN** the reported value may differ slightly from the value written
- **AND** the call is still reported as successful

### Requirement: Setting colour by RGB

`set_color` SHALL accept red, green, and blue components from 0 to 255 and apply them to the
bulb. A component outside that range SHALL be rejected as a validation error before any
request is sent.

#### Scenario: Setting a colour

- **WHEN** `set_color` is called with red 255, green 0, blue 0
- **THEN** the bulb shows red
- **AND** the result reports the applied colour

#### Scenario: Rejecting an out-of-range component

- **WHEN** `set_color` is called with a component above 255 or below 0
- **THEN** the call fails with a validation error naming the accepted range
- **AND** no request is sent to the bulb

### Requirement: Colour temperature validated against the device's range

`set_color_temp` SHALL accept a colour temperature in Kelvin and SHALL validate it against
the range the bulb itself reports as supported, rather than a hard-coded range.

A value outside the bulb's reported range SHALL be rejected as a validation error stating the
supported range, without a request being sent to the bulb.

#### Scenario: Setting a supported colour temperature

- **WHEN** `set_color_temp` is called with a Kelvin value inside the bulb's reported range
- **THEN** the bulb's colour temperature is set to that value

#### Scenario: Rejecting a colour temperature the bulb cannot produce

- **WHEN** `set_color_temp` is called with a Kelvin value outside the bulb's reported range
- **THEN** the call fails with a validation error stating the supported range
- **AND** no request is sent to the bulb

#### Scenario: Device range is unknown

- **WHEN** `set_color_temp` is called before the bulb's supported range has ever been
  determined, because it has not yet been reachable
- **THEN** the call fails with an error explaining that the bulb's capabilities are not yet
  known

### Requirement: Scene selection by name

`set_scene` SHALL accept a scene name, not a numeric scene identifier. Names SHALL be matched
without regard to letter case.

An unrecognised name SHALL be rejected with an error that names the available scenes, so an
agent can correct itself without a separate lookup.

`list_scenes` SHALL return the names of all scenes the bulb supports.

#### Scenario: Selecting a scene by name

- **WHEN** `set_scene` is called with "Cozy"
- **THEN** the bulb switches to that scene

#### Scenario: Scene name case does not matter

- **WHEN** `set_scene` is called with "cozy" or "COZY"
- **THEN** the bulb switches to the same scene as the correctly cased name

#### Scenario: Unrecognised scene name

- **WHEN** `set_scene` is called with a name the bulb does not support
- **THEN** the call fails with an error listing the available scene names
- **AND** no request is sent to the bulb

#### Scenario: Listing scenes

- **WHEN** `list_scenes` is called
- **THEN** the supported scene names are returned

### Requirement: Unsupported features are rejected clearly

When the bulb reports that it does not support a feature, a tool depending on that feature
SHALL fail with an error saying the device does not support it, rather than silently doing
nothing or reporting success.

#### Scenario: Colour requested on a bulb without colour support

- **WHEN** `set_color` is called and the bulb reports no colour support
- **THEN** the call fails with an error stating the device does not support colour
