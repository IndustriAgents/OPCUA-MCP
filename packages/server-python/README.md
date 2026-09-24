# OPC UA MCP Server

A Model Context Protocol (MCP) server that provides seamless integration with OPC UA servers. This server enables AI assistants and other MCP clients to interact with industrial automation systems through standardized OPC UA communication protocols.

There is a [Node runtime](https://github.com/IndustriAgents/OPCUA-MCP/tree/main/packages/server-node) exposing the same tools from the same contract. Both are first-class: one test suite runs against both, and they are released together under one version. Pick whichever stack you already run; the few declared differences between them, and the known divergences still being fixed, are listed in [docs/compatibility.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/compatibility.md#runtime-differences).

## Overview

This MCP server acts as a bridge between AI assistants and OPC UA servers, allowing for:
- Reading sensor data and system variables, with the engineering unit and range
  the node publishes, so a `51.75` arrives labelled °C rather than bare
- Writing control values to actuators and systems, bounded by the range the
  equipment itself declared
- Browsing OPC UA node hierarchies, with each node's type definition
- Calling OPC UA methods for system operations
- Subscribing to data changes, so a node can be watched rather than polled,
  with an optional deadband so jitter is discarded at the server
- Collecting events, listing the alarms a server retains, and acknowledging,
  confirming, annotating or shelving them
- Reading history — values, raw or summarised by a server-side aggregate, and
  events the server stored for a range that has already passed
- Reporting connection and server health, including the OPC UA server's own
  diagnostics counters, and reconnecting by itself when the server is restarted

One OPC UA endpoint per process: `OPCUA_SERVER_URL` is read once, every tool
targets it, and each MCP client opens its own OPC UA session.

## Tools

Fifteen tools, defined once in
**[contract/tools.json](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/tools.json)**
and shared with the Node runtime so the two cannot drift apart.

| Tool | What it does |
|---|---|
| `read_opcua_nodes` | Read one or more nodes — value, data type, status, timestamps, engineering unit and range |
| `browse_opcua_nodes` | List children, walk a subtree, resolve a browse path, search by name |
| `write_opcua_nodes` | Write to one or more nodes |
| `call_opcua_method` | Invoke a method on an object node |
| `get_server_status` | Connection state, server health, the namespace array and the server's own diagnostics |
| `subscribe_opcua_nodes` | Watch nodes for data changes instead of polling them, with an optional deadband |
| `list_subscriptions` | The active subscriptions, each with its buffered changes |
| `unsubscribe_opcua_nodes` | Cancel subscriptions |
| `subscribe_events` | Start collecting events from a notifier node |
| `read_events` | Read the events collected since the last read |
| `list_active_alarms` | The alarms the server is currently retaining |
| `acknowledge_alarm` | Acknowledge one of them, with a comment |
| `act_on_alarm` | Confirm, annotate or shelve an alarm — the rest of the operator workflow |
| `read_opcua_history` † | Historical values, raw or summarised by a server-side aggregate |
| `read_event_history` † | Events the server stored, for a range that has already passed |

† **Capability-gated.** `read_opcua_history` appears only when the connected
server advertises historical access (`AccessHistoryDataCapability`) or aggregates
(a non-empty `AggregateFunctions` folder). `read_event_history` is gated
separately on `AccessHistoryEventsCapability` — keeping values and keeping events
are different features and a server commonly does one without the other. What a
server cannot do is not on the menu, rather than failing at call time.

**One tool per operation, not one per arity.** Reading one node and reading fifty
is the same request with a longer list, so it is one tool and one code path.

**Bounded, and honest about it.** Every request is bounded before it reaches the
OPC UA server — at most 500 nodes per read, 100 per write, 64 method arguments,
1 MiB of arguments, and fewer where the OPC UA server publishes lower
`OperationLimits`; the full list is in the
[README](https://github.com/IndustriAgents/OPCUA-MCP#how-much-one-call-may-ask-for).
A request over a bound is refused whole, never partly sent. And every result that
can be partial — history, events, browse, subscriptions — carries a
`completeness` object beside `result` in `structuredContent`: test
`completeness.complete`, and never infer it from how many records came back.

See the central per-tool reference in **[docs/examples.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md)** for full tool signatures, parameters and return formats.

## Resources

One resource, `opcua://subscriptions`: the active data-change subscriptions and the values each has buffered, as JSON. It is the same set of records `list_subscriptions` returns, re-readable without spending a tool call. See [Subscriptions](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md#data-change-subscriptions) for the shape and the worked example.

## Features

### Key Capabilities

- **Automatic Connection Management**: Handles OPC UA client lifecycle with proper connection setup and teardown, and rebuilds a dead session rather than staying down until restart
- **Type-Safe Operations**: Automatic type conversion based on existing node data types, with values checked against the node's published range and any policy bounds before the write is sent
- **Tool Profiles and Audit**: An observe-only default, an allowlist for control, and every control call recorded with its targets, outcome, endpoint and session
- **Error Handling**: Comprehensive error reporting for debugging and monitoring; a failure never comes back as an empty message
- **Async Support**: Built on the `mcp` SDK's `MCPServer` for efficient asynchronous operations
- **Data-Change Subscriptions**: Monitored items buffered as they arrive, re-created on a new session after a reconnect
- **Configurable**: Environment-based endpoint, security policy, credentials and authorization policy

## Installation

### Prerequisites

- Python 3.10 or higher
- Access to an OPC UA server (local or remote)
- UV package manager (recommended) or pip

### From PyPI (recommended)

```bash
uvx opcua-mcp-server            # run without installing
uv tool install opcua-mcp-server   # or install permanently
pip install opcua-mcp-server       # or with pip
```

Point it at your OPC UA endpoint:

```bash
export OPCUA_SERVER_URL="opc.tcp://localhost:4840"
```

### Registering with Claude Desktop

Rather than editing `claude_desktop_config.json` by hand, let the server write it:

```bash
opcua-mcp-server --install claude-desktop --url opc.tcp://192.168.0.10:4840
```

It merges into the existing config, backs the old one up, and records absolute
paths — Claude Desktop is launched from the GUI and does not inherit a login
shell's `PATH`, so a bare `"command": "uvx"` often works in a terminal and fails
in the app. Add `--dry-run` to see the result first, `--force` to replace an
existing `opcua` entry.

There is also a **downloadable `.mcpb` bundle** for Claude Desktop — which
carries the Node runtime, because Claude Desktop supplies Node rather than
Python — and **single-file executables** of this runtime that need no Python at
all — see
[docs/install.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/install.md).

In an MCP client:

```json
{
  "mcpServers": {
    "opcua": {
      "command": "uvx",
      "args": ["opcua-mcp-server"],
      "env": { "OPCUA_SERVER_URL": "opc.tcp://localhost:4840" }
    }
  }
}
```

### From source (development)

1. **Install dependencies for the whole workspace (run from the repo root):**
   ```bash
   uv sync --all-packages
   ```

2. **Configure the OPC UA server URL:**
   ```bash
   export OPCUA_SERVER_URL="opc.tcp://localhost:4840"
   ```

## Configuration

The server is configured entirely through environment variables. Every one is
declared, with its type, default and secrecy, in the repository's
[`contract/config.json`](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/config.json),
which both runtimes are tested against and which ships inside this package:

| Variable | Default | Meaning |
|---|---|---|
| `OPCUA_SERVER_URL` | `opc.tcp://localhost:4840` | OPC UA endpoint to connect to |
| `OPCUA_SECURITY_POLICY` | `None` | `None`, `Basic128Rsa15`, `Basic256` or `Basic256Sha256` (the AES suites are Node-only) |
| `OPCUA_SECURITY_MODE` | `SignAndEncrypt` once a policy is set, otherwise `None` | `None`, `Sign` or `SignAndEncrypt` |
| `OPCUA_CLIENT_CERT` | — | Client certificate (PEM/DER). Required for any policy other than `None` |
| `OPCUA_CLIENT_KEY` | — | Private key for `OPCUA_CLIENT_CERT` |
| `OPCUA_APPLICATION_URI` | the `subjectAltName` URI of `OPCUA_CLIENT_CERT` | Application URI announced to the server. Set it only for a certificate that carries no URI of its own |
| `OPCUA_SERVER_CERT` | — | The OPC UA **server's** certificate, pinned. Without it, encryption protects against eavesdropping but not against an impostor endpoint, and **control tools are refused**. Requires a policy other than `None`; an expired pin refuses to connect |
| `OPCUA_USERNAME` | — | Username identity; the session is anonymous when unset |
| `OPCUA_PASSWORD` | — | Password for `OPCUA_USERNAME` |
| `OPCUA_USER_CERT` | — | Certificate identifying the **user**, for X.509 authentication. A different key pair from `OPCUA_CLIENT_CERT`, which secures the channel. Cannot be combined with `OPCUA_USERNAME` |
| `OPCUA_USER_KEY` | — | Private key for `OPCUA_USER_CERT`. Signs the server's challenge; never sent |
| `OPCUA_PROFILE` | `observe` | `observe`, `operator`, or `full` tool profile (`read-only` is an alias for `observe`) |
| `OPCUA_POLICY_FILE` | — | Optional version-1 JSON policy file; environment variables override it |
| `OPCUA_ALLOWED_TOOLS` | — | Comma-separated allowlist that can only narrow the selected profile |
| `OPCUA_ALLOWED_WRITE_NODES` | — | Comma-separated node IDs writable by the `operator` profile. `ns=2;i=5` or, preferably, `nsu=<namespace-uri>;i=5` |
| `OPCUA_ALLOWED_METHODS` | — | Comma-separated `object_node_id\|method_node_id` pairs callable by `operator` |
| `OPCUA_ALLOW_ACKNOWLEDGE_ALARMS` | `false` | Allow `operator` to act on alarms — `acknowledge_alarm` and every `act_on_alarm` action |
| `OPCUA_ALLOW_INSECURE_CONTROL` | `false` | Lab-only override permitting control tools over a channel with **no** security (`SecurityPolicy=None`). Does not cover an unverified server on a secured one |
| `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL` | `false` | Lab-only override permitting control tools over a secured channel whose server certificate is **not pinned** — encrypted, but to whoever answered |
| `OPCUA_ALLOW_OUT_OF_RANGE_WRITES` | `false` | Allow a write outside the `EURange` the OPC UA server itself published for that node |
| `OPCUA_AUDIT_FILE` | — | Append-only file for the control audit trail, one JSON object per line, written *beside* stderr. Created `0600`; a symlink, non-regular file, another account's file or a group/world-writable one stops the server, as does a file that cannot be opened. Reopened safely when rotated away. See [What is audited](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/SECURITY.md#what-is-audited) |
| `OPCUA_AUDIT_FSYNC` | `always` | `always` fsyncs each audit record before the control call proceeds (survives power loss); `none` stops at the OS (survives a process crash only) |
| `OPCUA_AUDIT_CHAIN` | `none` | `sha256` or `hmac-sha256` adds `seq`/`prev_hash`/`hash` to every record so edits, deletions and reordering are detectable with `--verify-audit`. Requires `OPCUA_AUDIT_FILE` |
| `OPCUA_AUDIT_CHAIN_KEY_FILE` | — | HMAC key (≥ 32 bytes, e.g. `openssl rand -hex 32`, mode `0600`) for `OPCUA_AUDIT_CHAIN=hmac-sha256`; refused with any other chain |
| `OPCUA_OPERATOR_ID` | — | Label stamped on every audit record as `operator_label`, so a shipped log says which deployment a control call came from. Configured, never verified — it is not an identity |
| `OPCUA_RECONNECT_INITIAL_DELAY_MS` | `1000` | Delay before the first reconnection attempt; doubles each attempt |
| `OPCUA_RECONNECT_MAX_DELAY_MS` | `8000` | Ceiling for that doubling |
| `OPCUA_RECONNECT_MAX_RETRY` | `3` | Retries after the first attempt, per connection round: a whole number from `-1` to `1000`. `0` disables retrying; `-1` never stops trying, but still in bounded rounds of four retries |
| `OPCUA_SESSION_TIMEOUT_MS` | `60000` | Session timeout asked of the OPC UA server; also sets the keep-alive period |

Names are case-insensitive, and a policy on its own implies `SignAndEncrypt`.
An unusable combination — a mode without a policy, a policy without a
certificate, a username without a password — is refused at startup with a
message naming the variable. With no security configured the connection is
unencrypted and unauthenticated, and the server says so on stderr; see
[SECURITY.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/SECURITY.md).
Making a client certificate and getting it trusted:
[docs/certificates.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/certificates.md).

On the **Python runtime**, certificate and key files are parsed as PEM only when
they are named `*.pem` and as DER otherwise (a `python-opcua` rule), so a PEM key
called `client.key` fails to load — name it `client_key.pem`. The Node runtime
sniffs the contents and accepts either name.

```bash
export OPCUA_SERVER_URL="opc.tcp://plc.example.internal:4840"
export OPCUA_SECURITY_POLICY="Basic256Sha256"     # implies SignAndEncrypt
export OPCUA_CLIENT_CERT="/etc/opcua/client.pem"
export OPCUA_CLIENT_KEY="/etc/opcua/client_key.pem"
export OPCUA_SERVER_CERT="/etc/opcua/plc_server.pem"
export OPCUA_USERNAME="mcp-operator"
export OPCUA_PASSWORD="…"
```

### Deciding what the agent may do

Three profiles, and the default is the restrictive one:

| `OPCUA_PROFILE` | What it offers |
|---|---|
| `observe` *(default)* | Read, browse, history and monitoring. No writes, no methods |
| `operator` | The above, plus **only** the write targets and methods you allowlist |
| `full` | Every tool |

`operator` is the one worth understanding. A write to a node outside
`OPCUA_ALLOWED_WRITE_NODES` is refused before anything reaches OPC UA, and one
forbidden target rejects an entire batch rather than letting part of it through.
Both `operator` and `full` also require a **verified server**: a security
policy _and_ the server's certificate pinned with `OPCUA_SERVER_CERT`. Encrypted
is not enough — without the pin the channel is encrypted to whoever answered.
Two lab-only overrides say otherwise in as many words, one per missing property:
`OPCUA_ALLOW_INSECURE_CONTROL=true` for a channel with no security, and
`OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL=true` for a secured one to an unpinned
server. A refused control call names the variable to set, and
`get_server_status` → `server_identity` says which of the three is in force.

The policy is enforced again on **every call**, not only when tools are listed —
an MCP client may hold a stale catalogue, and a hidden tool is a usability
feature rather than a security boundary.

A `writable_nodes` entry in a policy file may also bound the **value**, not only
the node: `min`, `max`, `enum` and `max_change` narrow what an allowlisted node
will accept, so a model that picks the right node and hallucinates `9999`
instead of `99.9` is refused. The node's own published `EURange` applies on top,
which is the only value bound that exists with no policy file at all. See
[Bounding the value, not only the node](https://github.com/IndustriAgents/OPCUA-MCP#bounding-the-value-not-only-the-node).

Every control call is recorded — its targets, its outcome, the endpoint it went
to, the session it rode on and the attempt number — to stderr and, with
`OPCUA_AUDIT_FILE` set, to an owner-only, append-only JSON-lines file beside it,
fsync'd per record. A control call whose record cannot be written is refused
rather than sent; reads carry on. `OPCUA_AUDIT_CHAIN` adds an optional hash
chain, checked with `opcua-mcp-server --verify-audit FILE… [--key-file KEY]`.
What the file can and cannot prove is in
[SECURITY.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/SECURITY.md#what-the-local-audit-file-can-and-cannot-prove).

### Staying connected

The connection is re-established by itself: a dropped or refused session is
retried with exponential backoff, and the read and write paths rebuild a dead
session rather than failing until the process is restarted. `python-opcua` has no
reconnection of its own, so this server owns the whole of it — including building
a fresh client per attempt, because a restarted server may present a new
certificate. Data-change subscriptions are re-created on the new session, so the
IDs keep working and the values already buffered are still there to be read. Tune
it with `OPCUA_RECONNECT_INITIAL_DELAY_MS`, `OPCUA_RECONNECT_MAX_DELAY_MS`,
`OPCUA_RECONNECT_MAX_RETRY` and `OPCUA_SESSION_TIMEOUT_MS` above.

Concurrent callers during an outage share one rebuild rather than each running
their own backoff, and a caller arriving mid-rebuild takes that attempt's answer.

The MCP server starts whether or not the OPC UA server is reachable: it answers
`initialize` at once and makes its first connection in the background, so an
endpoint that is down is reported rather than holding the MCP client back for a
whole round of backoff.

`get_server_status` reports whether the connection is up and what the OPC UA
server says about itself; it is the one tool that answers while the connection is
down, and calling it is also what brings a dropped one back. See
[Staying connected](https://github.com/IndustriAgents/OPCUA-MCP#staying-connected).

A control call whose session died before its response arrived is **not** silently
re-sent. OPC UA lets a Write partially succeed and defines no operation order, so
a dead session never proves the write did not land: the server rebuilds the
connection and then says plainly that the request may or may not have reached the
plant, naming what it was aimed at.

## Usage

### Running the Server

After `uv sync --all-packages` from the repo root:
```bash
uv run --no-sync opcua-mcp-server
```

Or run it directly against this package from anywhere in the repo:
```bash
uv --directory packages/server-python run opcua-mcp-server
```

### Integration with MCP Clients

Add to your MCP client configuration (e.g., `config.json`):

```json
{
  "mcpServers": {
    "opcua-mcp": {
      "command": "/path/to/uv",
      "args": [
        "--directory",
        "/path/to/packages/server-python",
        "run",
        "opcua-mcp-server"
      ],
      "env": {
        "OPCUA_SERVER_URL": "opc.tcp://localhost:4840"
      }
    }
  }
}
```

## Example Usage in Conversation

Once configured, you can ask Claude to perform real-world OPC UA operations:

### Reading Sensor Values
- "What is the current temperature reading from the reactor vessel?"
- "Check the pressure level in Tank A"
- "Read all temperature sensors in the cooling system"
- "Get the current flow rate on production line B"

### Controlling Equipment
- "Turn on the conveyor motor"
- "Set the mixer speed to 80 RPM"
- "Open valve V-101 to 75% position"
- "Start the circulation pump"
- "Set the heating element to 150°C"

### Alarms
- "What alarms are active right now?"
- "Acknowledge the high-temperature alarm with a note about the cause"
- "Shelve that chattering level alarm for thirty minutes"
- "What fired overnight on the packaging line?"

### System Monitoring
- "Read all sensor values from the water treatment plant"
- "Check the status of all pumps and motors"
- "Is the plant server refusing our connections, or just slow?"
- "Watch the reactor temperature and ignore changes under 0.5°C"
- "What variables are available on this OPC UA server?"

### Real Example Scenarios
```
User: "What's the temperature in reactor R-101?"
Assistant: I'll read the temperature sensor for reactor R-101.
Tool call: read_opcua_nodes with node_ids ["ns=2;i=11"]
Result: "Reactor R-101 temperature: 87.3 °C (range 0–150 °C)"

User: "Turn on the main conveyor motor"
Assistant: I'll start the main conveyor motor for you.
Tool call: write_opcua_nodes with nodes [{node_id: "ns=2;i=12", value: true}]
Result: "Main conveyor motor started successfully"

User: "Set the pump speed to 65%"
Assistant: I'll adjust the pump speed to 65%.
Tool call: write_opcua_nodes with nodes [{node_id: "ns=2;i=13", value: 65}]
Result: "Pump speed set to 65%"

User: "What variables are available on this OPC UA server?"
Assistant: I'll browse the OPC UA server's address space.
Tool call: browse_opcua_nodes
Result: Found 5 variables:
- Temperature (ns=2;i=2): 25.3 °C - AnalogItemType
- Pressure (ns=2;i=3): 5.0 bar - AnalogItemType
- MotorSpeed (ns=2;i=4): 1500 RPM - AnalogItemType
- MotorState (ns=2;i=5): True - Motor ON/OFF state
- ValvePosition (ns=2;i=6): False - Valve OPEN/CLOSED position
```

## Security

The connection defaults to no security (`SecurityPolicy.None`) and the tool
profile defaults to **observe-only**. For anything beyond local development,
configure both channel security and an `operator` allowlist, and pin the endpoint
with `OPCUA_SERVER_CERT`. Control tools need that pin, not only an encrypted
channel: encryption says nobody can read the traffic, the pin says who is on the
other end. Each can be waived for a lab by its own explicit override
(`OPCUA_ALLOW_INSECURE_CONTROL`, `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL`), and
never by the other's.

The MCP policy is defense in depth, not a replacement for OPC UA authorization.
Scope the OPC UA account to the same nodes and methods; use a separate read-only
account for `observe` deployments.

See [SECURITY.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/SECURITY.md)
for the full posture, what the servers do and do not verify, and how to report a
vulnerability.

## API Reference

See the central per-tool reference in **[docs/examples.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md)** for full tool signatures, parameters, and return formats. The shared tool surface is defined in **[contract/tools.json](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/tools.json)**.
