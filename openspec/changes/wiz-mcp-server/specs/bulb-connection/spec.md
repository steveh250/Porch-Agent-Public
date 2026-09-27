# Spec Delta

## Purpose

Governs the lifecycle of the server's single bulb connection, including how reachability is
tracked while the bulb may be powered off and how device failures become errors an agent can
act on.

## ADDED Requirements

### Requirement: Connection is established lazily

The server SHALL NOT require the bulb to be reachable in order to start. It SHALL NOT attempt
a blocking device handshake during startup, and a failure to reach the bulb SHALL NOT prevent
the server from serving requests.

#### Scenario: Server starts with the bulb powered off

- **WHEN** the server is started while the bulb is powered off
- **THEN** the server starts successfully and serves its tool list
- **AND** tools that do not need the device, such as `get_light_state`, still respond

#### Scenario: Bulb becomes reachable after startup

- **WHEN** the bulb is powered on some time after the server started
- **THEN** the server begins reporting it as reachable without needing a restart

### Requirement: Background presence polling

The server SHALL poll the bulb on a configured interval to determine whether it is reachable
and to refresh its known state. Polling SHALL begin when the server starts and stop cleanly
when it shuts down.

A poll that fails SHALL NOT stop the polling loop; polling SHALL continue so the bulb is
detected when it returns.

#### Scenario: Polling detects the bulb going away

- **WHEN** a reachable bulb is powered off and a poll interval elapses
- **THEN** the server reports the bulb as not reachable

#### Scenario: Polling detects the bulb returning

- **WHEN** an unreachable bulb is powered on and a poll interval elapses
- **THEN** the server reports the bulb as reachable
- **AND** reports its refreshed state

#### Scenario: Repeated poll failures do not stop polling

- **WHEN** many consecutive polls fail
- **THEN** polling continues on the configured interval
- **AND** the server remains able to serve requests

#### Scenario: Polling stops on shutdown

- **WHEN** the server is shut down
- **THEN** the polling task stops and the process exits without hanging

### Requirement: Poll results serve as the state cache

The most recent successful poll result SHALL be the state that `get_light_state` reports, so
that reading state never requires a device round trip.

The server SHALL record when the cached state was last confirmed, and SHALL distinguish a
bulb that has never been reached from one that was reachable earlier and is not now.

A successful write to the bulb SHALL update the cached state, so that state read immediately
after a write reflects the change rather than waiting for the next poll.

#### Scenario: State read comes from cache

- **WHEN** state is requested between two polls
- **THEN** the last polled state is returned together with the time it was confirmed
- **AND** no device request is made

#### Scenario: Write updates the cache immediately

- **WHEN** the light is switched on and state is read immediately afterwards
- **THEN** the state reports the light as on without waiting for the next poll

#### Scenario: Bulb has never been reached

- **WHEN** state is requested and the bulb has not been reachable since the server started
- **THEN** the result reports the bulb as not reachable
- **AND** indicates that no state has ever been confirmed

### Requirement: Device requests are bounded by a timeout

Every request to the bulb SHALL be bounded by a configured timeout, so that no tool call can
hang indefinitely on an unresponsive device.

#### Scenario: Unresponsive bulb during a write

- **WHEN** a tool that writes to the bulb is called and the bulb does not respond
- **THEN** the call fails within approximately the configured timeout
- **AND** returns an error saying the bulb did not respond

### Requirement: Device failures map to distinct, actionable errors

Failures reaching or operating the bulb SHALL be reported as structured errors that state what
failed and include the bulb's configured address. The server SHALL distinguish at least:

- the bulb not responding within the timeout,
- the bulb being unreachable on the network,
- the device being an unrecognised model,
- the device rejecting an unsupported operation.

An unexpected internal failure SHALL be reported as an error rather than crashing the server,
and SHALL NOT terminate the process or the polling loop.

#### Scenario: Timeout is distinguishable from unreachability

- **WHEN** a request times out
- **THEN** the error identifies a timeout rather than a generic failure

#### Scenario: Error names the configured bulb address

- **WHEN** any device failure is reported
- **THEN** the error includes the bulb's configured address, so a misconfiguration is visible

#### Scenario: A tool failure does not take down the server

- **WHEN** a tool call fails because of a device error
- **THEN** the server continues running and continues to accept further calls
