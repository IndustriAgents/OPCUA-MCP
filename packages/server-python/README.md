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

<!-- BEGIN GENERATED: tool-reference from contract/tools.json by packages/server-node/scripts/config-artifacts.mjs. Do not edit by hand: edit the source, then run `npm run config:generate` in packages/server-node. -->

Both runtimes expose the same **15 tools** — 7 read, 4 monitor, 2 alarm-action and 2 control — defined once in [`contract/tools.json`](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/tools.json).

| Tool | Access | Hints | What it does |
|---|---|---|---|
| `read_opcua_nodes` | read | read-only, idempotent | Read the current value of one or more OPC UA nodes in a single request. |
| `browse_opcua_nodes` | read | read-only, idempotent | Explore the OPC UA address space: list a node's children, walk a subtree, resolve a human-readable browse path to a node ID, or search for nodes by name. |
| `read_opcua_history` † | read | read-only, idempotent | Read what a node's value has been over time. |
| `read_event_history` † | read | read-only, idempotent | Read events the OPC UA server stored, for a time range that has already passed. |
| `get_server_status` | read | read-only, idempotent | Report whether this MCP server is connected to the OPC UA server and what that server says about itself: its endpoint and connection security, its ServerStatus (state, current time, start time, build info) and its NamespaceArray as index -> URI. |
| `list_subscriptions` | read | read-only, idempotent | List the active OPC UA data-change subscriptions, each with the value changes buffered for it since it was created. |
| `list_active_alarms` | read | read-only, idempotent | List the alarm and condition instances the server is currently retaining — those that are active, unacknowledged, or both. |
| `subscribe_opcua_nodes` | monitor | — | Watch one or more OPC UA nodes for value changes instead of polling them. |
| `unsubscribe_opcua_nodes` | monitor | — | Cancel one or more active data-change subscriptions and discard what they had buffered. |
| `subscribe_events` | monitor | — | Start buffering OPC UA events (alarms, condition changes, plain events) from a notifier node. |
| `read_events` | monitor | — | Read the events buffered by subscribe_events, oldest first. |
| `acknowledge_alarm` | alarm-action | — | Acknowledge an alarm or condition, identified by the event_id of the event that reported it (from list_active_alarms or read_events). |
| `act_on_alarm` | alarm-action | — | Confirm, annotate or shelve an alarm — the rest of the operator workflow that acknowledge_alarm starts. |
| `write_opcua_nodes` | control | destructive, idempotent | Write a value to one or more OPC UA nodes. |
| `call_opcua_method` | control | destructive | Call a method on an OPC UA object. |

**Access** decides which `OPCUA_PROFILE` offers a tool. `read` and `monitor` tools are offered under every profile, including the default `observe`. `control` tools need `operator`, which offers them only for allowlisted targets, or `full`; `alarm-action` tools need `operator` with `OPCUA_ALLOW_ACKNOWLEDGE_ALARMS`, or `full`. Both also need a verified server — a secured channel and a pinned `OPCUA_SERVER_CERT` — unless a lab override is set: `OPCUA_ALLOW_INSECURE_CONTROL` for a channel with no security, `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL` for an unpinned server. **Hints** are the MCP tool annotations each tool advertises.

† **Needs a server feature**: always listed, and refused at call time with `capability_not_supported` or `capability_unknown` when the connected server does not advertise what it needs — `read_opcua_history` on `AccessHistoryDataCapability` or `AggregateFunctions`; `read_event_history` on `AccessHistoryEventsCapability`.

<!-- END GENERATED: tool-reference -->

`read_opcua_history` needs
historical access (`AccessHistoryDataCapability`) or aggregates (a non-empty
`AggregateFunctions` folder), and its `aggregate_function` argument needs the
latter. `read_event_history` needs `AccessHistoryEventsCapability` — keeping
values and keeping events are different features and a server commonly does one
without the other. Every tool is listed whatever the plant is doing, with the
same schema, because MCP clients keep the first list they get and nothing tells
them reliably to ask again. A call the connected server cannot serve is refused
before anything is sent, with a code to act on — `capability_not_supported`,
`capability_unknown`, or `endpoint_offline` when the server cannot be reached —
and what to use instead. `get_server_status` → `capabilities` reports what the
server was found to offer, including its aggregate function names, on which
session and when.

**One tool per operation, not one per arity.** Reading one node and reading fifty
is the same request with a longer list, so it is one tool and one code path.

**Bounded, and honest about it.** Every request is bounded before it reaches the
OPC UA server — at most 500 nodes per read, 100 per write, 64 method arguments,
1 MiB of arguments, and fewer where the OPC UA server publishes lower
`OperationLimits`; the full list is in the
[tools reference](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/tools.md#how-much-one-call-may-ask-for).
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

### Registering with Claude Desktop or Codex

Rather than editing `claude_desktop_config.json` (or Codex's `config.toml`) by
hand, let the server write it:

```bash
opcua-mcp-server --install claude-desktop --dry-run \
  --url opc.tcp://192.168.0.10:4840 \
  --security-policy Basic256Sha256 \
  --client-cert /etc/opcua/client.pem --client-key /etc/opcua/client_key.pem \
  --server-cert /etc/opcua/server.pem
```

`--dry-run` validates the files and the combination with the server's own
startup parsers and prints a redacted preview and a security summary; drop it to
write (`--install codex` for Codex). It merges into the existing config, backs the
old one up, and records absolute paths — Claude Desktop is launched from the GUI
and does not inherit a login shell's `PATH`, so a bare `"command": "uvx"` often
works in a terminal and fails in the app. The profile defaults to read-only; a
control profile must be chosen with `--profile`, and for a remote endpoint needs a
pinned `--server-cert`. Passwords are never accepted as flags. `--force` replaces
an existing `opcua` entry. Every flag and rule is in
[docs/install.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/install.md#3---install--let-the-server-write-the-config).

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

<!-- BEGIN GENERATED: config-reference from contract/config.json by packages/server-node/scripts/config-artifacts.mjs. Do not edit by hand: edit the source, then run `npm run config:generate` in packages/server-node. -->

29 settings in six groups. A blank value means the default, whatever the type; a boolean accepts `1`, `true`, `yes`, `on` and `0`, `false`, `no`, `off`.

**Connection** — Which OPC UA server to talk to.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_SERVER_URL` | `opc.tcp://localhost:4840` | URL of the OPC UA server to connect to, including any path the server expects. Read once at startup: one process serves one endpoint. Nothing verifies who answers at this address unless the server certificate is pinned. |

**Channel security** — How the OPC UA secure channel is signed, encrypted and verified.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_SECURITY_POLICY` | `None` | Encryption suite to negotiate for the secure channel. Anything other than None needs a client certificate and key. The two AES suites are Node-only: the Python runtime refuses them at startup rather than downgrading. One of `None`, `Basic128Rsa15`, `Basic256`, `Basic256Sha256`. None is unencrypted and unauthenticated, and keeps control tools blocked unless insecure control is explicitly allowed. An unknown policy stops the server at startup. |
| `OPCUA_SECURITY_MODE` | SignAndEncrypt once a security policy is set, otherwise None | Message security mode for the secure channel. Sign authenticates messages without encrypting them. One of `None`, `Sign`, `SignAndEncrypt`. A policy with mode None, or a mode without a policy, stops the server at startup; leaving it blank never downgrades a policy to signing only. |
| `OPCUA_CLIENT_CERT` | — | Path to the certificate (PEM or DER) this client presents as its application identity. Required by any security policy other than None. A path that does not exist stops the server at startup. |
| `OPCUA_CLIENT_KEY` | — | Path to the private key matching the client certificate. A path, never key material. Whoever can read the file can impersonate this client: keep it readable only by the account running the server. A path that does not exist stops the server at startup. |
| `OPCUA_APPLICATION_URI` | the subjectAltName URI of the client certificate | Application URI announced to the server. Set it only for a certificate that carries no URI of its own: a server may reject a session whose URI does not match the certificate. |
| `OPCUA_SERVER_CERT` | — | Path to the OPC UA server's own certificate (PEM or DER), pinned: a server presenting any other certificate cannot complete the handshake. Control tools (writes, method calls, alarm actions) require it. Requires a security policy other than None. Unset, encryption protects against eavesdropping but not against an impostor endpoint, so control tools are refused unless OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL is set. Set without a security policy, it stops the server at startup rather than pinning nothing; a pinned certificate that has expired or is not yet valid refuses to connect. |

**User identity** — Who the OPC UA session logs in as.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_USERNAME` | — | OPC UA username. The session is anonymous when unset. Cannot be combined with a user certificate. Requires a password; either one alone stops the server at startup. |
| `OPCUA_PASSWORD` | — | **Secret.** Password for the username. Without a security policy it crosses the network in clear text. Supply it through the client's secret storage, never on a command line. |
| `OPCUA_USER_CERT` | — | Path to the certificate identifying the user, for X.509 user authentication: a different key pair from the client certificate, which secures the channel. Requires the user key and a security policy other than None; cannot be combined with a username. Any of those combinations broken stops the server at startup. |
| `OPCUA_USER_KEY` | — | Path to the private key for the user certificate. It signs the server's challenge and is never sent. Whoever can read the file can log in as this user: keep it readable only by the account running the server. |

**Tool policy** — Which MCP tools are offered, and what a control tool may touch.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_PROFILE` | `observe` | Which tools are offered. observe reads, browses, reads history and monitors; operator adds explicitly allowlisted writes, method calls and alarm actions; full exposes every tool and is meant only for a tightly scoped OPC UA account. One of `observe`, `operator`, `full` (`read-only` means `observe`; `readonly` means `observe`). Unset fails closed to observe; an unknown profile stops the server at startup. |
| `OPCUA_POLICY_FILE` | — | Path to a version-1 JSON policy file, the only place per-node value bounds can be set. Every policy variable that is set overrides what the file says. An unreadable or invalid file stops the server at startup. |
| `OPCUA_ALLOWED_TOOLS` | — | Comma-separated tool allowlist. It can only narrow the selected profile, never widen it. An unknown tool name stops the server at startup. |
| `OPCUA_ALLOWED_WRITE_NODES` | — | Comma-separated exact node IDs the operator profile may write, as ns=2;i=5 or, stable across a server restart, nsu=&lt;namespace-uri>;i=5. Setting it replaces the policy file's list, bounds included. Empty fails closed: operator can write nothing. A batch write with any target outside the list is refused before it reaches OPC UA. |
| `OPCUA_ALLOWED_METHODS` | — | Comma-separated object_node_id\|method_node_id pairs the operator profile may call. Empty fails closed: operator can call nothing. An entry without \| stops the server at startup. |
| `OPCUA_ALLOW_ACKNOWLEDGE_ALARMS` | `false` | Let the operator profile act on alarms: acknowledge_alarm and every act_on_alarm action. |
| `OPCUA_ALLOW_INSECURE_CONTROL` | `false` | Permit control tools on an OPC UA channel with no security policy (None). Does not cover a secured channel to an unverified server; that is OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL. Fail-open override: writes, method calls and alarm actions then travel unauthenticated and unencrypted. Reported as control=INSECURE-OVERRIDE in the startup line, get_server_status and every audit record. Never enable it against production equipment. |
| `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL` | `false` | Permit control tools on a secured OPC UA channel whose server certificate is not pinned with OPCUA_SERVER_CERT. Does not cover a channel with no security policy; that is OPCUA_ALLOW_INSECURE_CONTROL. Fail-open override: writes, method calls and alarm actions are then encrypted to whichever server answered the endpoint, which may be an impostor. Reported as control=UNVERIFIED-OVERRIDE in the startup line, get_server_status and every audit record. Never enable it against production equipment. |
| `OPCUA_ALLOW_OUT_OF_RANGE_WRITES` | `false` | Allow a write outside the EURange the OPC UA server itself published for the node. Fail-open override: it discards the only value bound a deployment with no policy file has. |

**Audit** — Where the control audit trail goes, and what it is labelled with.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_AUDIT_FILE` | — | Path to an append-only file for the control audit trail, one JSON object per line, written beside stderr. Created owner-only (0600) if it does not exist; reopened when rotated away. A file that cannot be opened, or that is a symlink, not a regular file, another account's, or group/world-writable, stops the server rather than falling back to stderr alone. While set, a control call whose record cannot be written is refused (fail closed); reads continue. |
| `OPCUA_AUDIT_FSYNC` | `always` | When an audit-file record counts as written. always fsyncs each record before the control call proceeds, so it survives a power loss; none leaves it to the operating system, so it survives only a crash of this process. One of `always`, `none`. none can lose the records of control calls that already reached the plant if the machine loses power. |
| `OPCUA_AUDIT_CHAIN` | `none` | Adds seq, prev_hash and hash to every audit record so edits, deletions and reordering can be detected with --verify-audit. sha256 catches accidental or naive edits; hmac-sha256 also resists anyone without the key. Requires the audit file. One of `none`, `sha256`, `hmac-sha256`. Set without an audit file, or hmac-sha256 without a key file, stops the server. No chain detects a truncated tail. |
| `OPCUA_AUDIT_CHAIN_KEY_FILE` | — | Path to the HMAC key for the hmac-sha256 audit chain: at least 32 bytes, for example from openssl rand -hex 32. Surrounding whitespace is ignored. Whoever can read the key can forge the chain: a key readable or writable by group or others stops the server. Refused unless the chain is hmac-sha256. |
| `OPCUA_OPERATOR_ID` | — | Label stamped on every audit record as operator_label, so a shipped log says which deployment a control call came from. Configured, never verified: it is not an identity, and mcp_principal stays null. |

**Reconnection** — How a refused or dropped connection is retried.

| Variable | Default | Description |
|---|---|---|
| `OPCUA_RECONNECT_INITIAL_DELAY_MS` | `1000` | Wait before the first attempt to repair a dropped or refused connection, in milliseconds. It doubles with each further attempt. |
| `OPCUA_RECONNECT_MAX_DELAY_MS` | `8000` | Ceiling for the doubling retry delay, in milliseconds. |
| `OPCUA_RECONNECT_MAX_RETRY` | `3` | Retries after the first attempt, per connection round, before a tool call gives up: a whole number from -1 to 1000. 0 never retries; -1 never stops trying, in bounded rounds of four retries so no single call waits forever. |
| `OPCUA_SESSION_TIMEOUT_MS` | `60000` | Session lifetime asked of the OPC UA server, in milliseconds. It also sets the keep-alive period, so it decides how quickly an idle connection notices the server has gone. |

<!-- END GENERATED: config-reference -->

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
[Bounding the value, not only the node](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/configuration.md#bounding-the-value-not-only-the-node).

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
IDs keep working and the values already buffered are still there to be read. Event
subscriptions are re-created too, and the next `read_events` notes that events
raised while the connection was down were not received. Tune
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
[Staying connected](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/configuration.md#staying-connected).

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
