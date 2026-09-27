# Spec Delta

## Purpose

Defines a standalone script that exercises every tool of a running server against a real bulb,
so an operator can confirm the integration works on hardware that no automated test can reach.

## ADDED Requirements

### Requirement: Harness is a standalone script driving the server as a client

The harness SHALL be a single Python script, separate from the server, that connects to an
already-running server over streamable-HTTP and drives it as an ordinary MCP client.

It SHALL NOT import the server's internals, start the server itself, or talk to the bulb
directly, so that what it validates is the MCP contract an agent would actually use.

#### Scenario: Harness exercises the server as a client

- **WHEN** the harness is run against a running server
- **THEN** it connects over streamable-HTTP, lists the tools, and calls them
- **AND** it does not communicate with the bulb except through the server

### Requirement: Harness exercises every tool and reports per-tool results

The harness SHALL call every advertised tool and report, for each one, whether it succeeded,
together with the value returned or the error raised.

It SHALL verify that the advertised tool list matches what is expected, so a missing or
misnamed tool is reported rather than silently skipped.

It SHALL finish with a summary and SHALL exit with a non-zero status if any check failed, so
it is usable non-interactively.

#### Scenario: All tools succeed

- **WHEN** the harness runs against a healthy server and a reachable bulb
- **THEN** every tool is reported as passing
- **AND** the script exits with a zero status

#### Scenario: A tool fails

- **WHEN** one tool returns an error
- **THEN** that tool is reported as failing with the error text
- **AND** remaining tools are still attempted
- **AND** the script exits with a non-zero status

#### Scenario: Tool list does not match

- **WHEN** an expected tool is absent from the server's advertised list
- **THEN** the harness reports it as missing rather than skipping it silently

### Requirement: Harness changes are visible on the real bulb and are undone

Because the harness drives real hardware, it SHALL make changes an operator can see with their
own eyes, pausing briefly between visible steps so each one can be observed.

It SHALL record the light's state before it begins and SHALL restore that state when it
finishes, including after a failure, so running it does not leave the porch light in an
unexpected state.

#### Scenario: Operator can observe each change

- **WHEN** the harness sets brightness, colour, colour temperature, and a scene in turn
- **THEN** each change is applied with a brief pause so it is visible on the bulb

#### Scenario: Original state is restored

- **WHEN** the harness completes, whether passing or failing
- **THEN** the light is returned to the state it was in before the run

### Requirement: Harness is self-contained and documents its own operation

The harness SHALL be runnable on a machine that has only this repository, its dependencies, and
network access to the bulb, with no assumptions about the development environment.

Its usage SHALL be documented so that an operator can start the server and run the harness by
following the instructions, and it SHALL take the server's address from configuration or a
command-line argument rather than a hard-coded value.

#### Scenario: Operator follows documented steps on the target machine

- **WHEN** an operator with only this repository and its dependencies follows the documented
  instructions
- **THEN** they can start the server and run the harness against the real bulb

#### Scenario: Server address is not hard-coded

- **WHEN** the server runs on a non-default host or port
- **THEN** the harness can be pointed at it through configuration or a command-line argument

### Requirement: Harness reports an unreachable server or bulb distinctly

The harness SHALL distinguish being unable to reach the server from the server reporting the
bulb as unreachable, because the corrective action differs.

#### Scenario: Server is not running

- **WHEN** the harness is run with no server listening
- **THEN** it reports that it could not connect to the server, and where it tried
- **AND** it does not report this as a bulb fault

#### Scenario: Bulb is powered off

- **WHEN** the harness runs against a healthy server whose bulb is powered off
- **THEN** it reports that the server is reachable but the bulb is not
