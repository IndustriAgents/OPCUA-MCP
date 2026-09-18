<div align="center">

# 🏭 OPC UA MCP Server

**Read industrial sensors and control equipment on any OPC UA server — through natural language with Claude and any MCP client.**

[![npm version](https://img.shields.io/npm/v/opcua-mcp-server)](https://www.npmjs.com/package/opcua-mcp-server)
[![PyPI version](https://img.shields.io/pypi/v/opcua-mcp-server)](https://pypi.org/project/opcua-mcp-server/)
[![npm downloads](https://img.shields.io/npm/dm/opcua-mcp-server)](https://www.npmjs.com/package/opcua-mcp-server)
[![CI](https://github.com/midhunxavier/OPCUA-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/midhunxavier/OPCUA-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/midhunxavier/OPCUA-MCP)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/midhunxavier/OPCUA-MCP?style=social)](https://github.com/midhunxavier/OPCUA-MCP)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org)
[![Node.js 22.13+](https://img.shields.io/badge/node-22.13+-green.svg)](https://nodejs.org)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[Quick Start](#quick-start) · [Install](docs/install.md) · [Tools](#tools) · [Examples](docs/examples.md) · [Compatibility](docs/compatibility.md) · [Architecture](docs/architecture.md) · [Testing](docs/testing.md) · [Roadmap](ROADMAP.md) · [Contributing](CONTRIBUTING.md)

</div>

![OPC UA MCP Server Screenshot](docs/assets/screenshot.png)

## Overview

Two interchangeable implementations — **Python** and **TypeScript/Node** — expose
the same OPC UA operations as MCP tools: read and write nodes, browse the address
space, call methods, read history and server-side aggregates, and subscribe to
events and alarms. Both speak plain OPC UA, so both connect to any server that
does; which tools are actually offered depends on the server's capabilities, the
permissions of the OPC UA account, and the configured
[tool profile](#configuration). Pick whichever runtime fits your stack.

Which servers and operations the test suite exercises, and which are only
reported by users, is set out in
**[docs/compatibility.md](docs/compatibility.md)**.

```mermaid
flowchart LR
    A["AI client<br/>(Claude Desktop / Code / Cursor)"] -->|MCP over stdio| B["OPC UA MCP Server<br/>(Python or Node)"]
    B -->|OPC UA| C["OPC UA Server<br/>(PLC / SCADA / mock)"]
```

## Quick Start

**Claude Desktop, nothing installed?** Download the `.mcpb` bundle from the
[latest release](https://github.com/midhunxavier/OPCUA-MCP/releases/latest) and
drag it into **Settings → Extensions**. It carries the server and every
dependency, Claude Desktop supplies the runtime, and the OPC UA endpoint is a
field in the settings form — no Node, no Python, no JSON to edit. There are
[single-file executables](docs/install.md#2-single-file-executable--no-runtime-at-all)
too, for machines with no runtime and no network.

**Already have a runtime?** Install the package and let it write the config:

```bash
npm install -g opcua-mcp-server        # or: uv tool install opcua-mcp-server
opcua-mcp-server --install claude-desktop --url opc.tcp://192.168.0.10:4840
```

**Prefer to configure it yourself?** Add one of these to your MCP client config
and point `OPCUA_SERVER_URL` at your OPC UA endpoint.

**Node** (via `npx`):

```json
{
  "mcpServers": {
    "opcua": {
      "command": "npx",
      "args": ["-y", "opcua-mcp-server"],
      "env": { "OPCUA_SERVER_URL": "opc.tcp://localhost:4840" }
    }
  }
}
```

**Python** (via [`uvx`](https://docs.astral.sh/uv/)):

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

For Claude Code, one command does it:

```bash
claude mcp add opcua -e OPCUA_SERVER_URL=opc.tcp://localhost:4840 -- npx -y opcua-mcp-server
```

All four routes, and what to do when Claude Desktop cannot start the server:
**[docs/install.md](docs/install.md)**.

> **No OPC UA server to hand?** This repo ships a mock industrial plant — see
> [Try it against the mock](#try-it-against-the-mock).

## Tools

Both servers expose the same seventeen tools, defined once in
[`contract/tools.json`](contract/tools.json) so they cannot drift apart.

| Tool | What it does |
|---|---|
| `read_opcua_node` | Read a single node's value |
| `write_opcua_node` | Write a value to a node |
| `read_multiple_opcua_nodes` | Batch read |
| `write_multiple_opcua_nodes` | Batch write |
| `browse_opcua_node_children` | List a node's children |
| `call_opcua_method` | Invoke a method on an object node |
| `get_all_variables` | Inventory every variable in the address space |
| `get_server_status` | Connection state, server health and the namespace array |
| `subscribe_opcua_node` | Watch a node for data changes instead of polling it |
| `list_subscriptions` | The active subscriptions, each with its buffered changes |
| `unsubscribe_opcua_node` | Cancel one subscription |
| `subscribe_events` | Start collecting events from a notifier node |
| `read_events` | Read the events collected since the last read |
| `list_active_alarms` | The alarms the server is currently retaining |
| `acknowledge_alarm` | Acknowledge one of them, with a comment |
| `read_history_opcua_node` † | Read historical, timestamped values |
| `read_aggregate_opcua_node` † | Server-computed aggregates (Average, Min, Max, …) |

† **Capability-gated.** These appear only when the connected server advertises
support — history via `AccessHistoryDataCapability`, aggregates via a non-empty
`AggregateFunctions` folder. Against a server without them, the tools are simply
not offered rather than failing at call time.

Both servers also expose one **resource**, `opcua://subscriptions`: the same
records `list_subscriptions` returns, re-readable without spending a tool call.

Full per-tool reference with inputs, outputs and a node-ID map:
**[docs/examples.md](docs/examples.md)**.

## Example usage in conversation

Once configured, you can ask in plain language:

- *"What's the current temperature reading from the reactor vessel?"*
- *"Set the valve position to 80%"*
- *"Show me all available variables in the system"*
- *"What was the temperature over the last hour?"*
- *"Start production on line 1 at 100 units/hour"*
- *"Give me the hourly average temperature for today"*
- *"Watch the tank level and tell me what it does over the next minute"*
- *"What alarms are active right now?"*
- *"Acknowledge the high-temperature alarm — I'm looking into it"*

Real responses from the bundled mock plant, via the published package:

```
read_opcua_node   node_id="ns=2;i=3"
→ Node ns=2;i=3 value: 23.101165241243347

write_opcua_node  node_id="ns=2;i=13"  value="80"
→ Successfully wrote 80 to node ns=2;i=13

get_all_variables
→ Found 22 variables:

  - Name: Temperature
    NodeID: ns=2;i=3
    Object ID: ns=2;i=2
    Value: 26.34449150525422
    Data Type: ns=0;i=11
    Description: Temperature
  …

read_history_opcua_node  node_id="ns=2;i=3"  num_values=2
→ { "value": 24.231991377989036, "timestamp": "2026-09-10T13:15:12.214Z", "status": "Good" }
  { "value": 26.089859958260515, "timestamp": "2026-09-10T13:15:11.208Z", "status": "Good" }

subscribe_opcua_node  node_id="ns=2;i=3"  publishing_interval=500
→ { "subscription_id": "sub-1", "node_id": "ns=2;i=3", "publishing_interval": 500,
    "sampling_interval": 500, "buffer_size": 20, "change_count": 0, "changes": [] }

list_subscriptions            # a few seconds later
→ { "subscription_id": "sub-1", …, "change_count": 4, "changes": [
      { "value": 25.33, "timestamp": "2026-09-10T13:15:11.478Z", "status": "Good" },
      { "value": 26.05, "timestamp": "2026-09-10T13:15:12.481Z", "status": "Good" }, … ] }

list_active_alarms
→ { "event_id": "ZjW7HJrVSFzDV2sMsX7sEQAAAAE=",
    "event_type": "ns=0;i=9341",
    "source_node": "ns=1;i=1001",
    "source_name": "Temperature",
    "time": "2026-09-10T13:15:12.214Z",
    "message": "Condition is 100.000 and state is High",
    "severity": 700,
    "condition_id": "ns=1;i=1002",
    "condition_name": "HighTemperatureAlarm",
    "active": true, "acked": false, "retain": true }

acknowledge_alarm  event_id="ZjW7HJrVSFzDV2sMsX7sEQAAAAE="  comment="on it"
→ Acknowledged alarm ns=1;i=1002 (event ZjW7HJrVSFzDV2sMsX7sEQAAAAE=)
```

Both runtimes return that same record shape — one record per historical value —
for `read_history_opcua_node` and `read_aggregate_opcua_node` alike, one for the
subscription family and one for the event family (`read_events`,
`list_active_alarms`). All three are defined in `contract/tools.json`
(`resultShapes`) and enforced against both servers by the test suite.

Events are collected, not pushed: MCP is request/response, so `subscribe_events`
starts a real OPC UA subscription in the background and `read_events` hands over
what has arrived since you last asked. `list_active_alarms` does not need one —
it asks the server for its retained conditions directly (ConditionRefresh), and
says so plainly when the server has no Alarms & Conditions support to ask.

Bad input is rejected identically by both runtimes:

```
read_history_opcua_node  node_id="ns=2;i=3"  start_time="2026-02-30T00:00:00Z"
→ Error: Invalid date/time: "2026-02-30T00:00:00Z". Use ISO 8601, e.g. 2026-04-23T17:40:00Z
```

## Configuration

Both runtimes read the same environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `OPCUA_SERVER_URL` | `opc.tcp://localhost:4840` | OPC UA endpoint to connect to |
| `OPCUA_SECURITY_POLICY` | `None` | `None`, `Basic128Rsa15`, `Basic256`, `Basic256Sha256` — plus `Aes128_Sha256_RsaOaep` and `Aes256_Sha256_RsaPss` on the Node runtime |
| `OPCUA_SECURITY_MODE` | `SignAndEncrypt` once a policy is set, otherwise `None` | `None`, `Sign` or `SignAndEncrypt` |
| `OPCUA_CLIENT_CERT` | — | Client certificate (PEM/DER). Required for any policy other than `None` |
| `OPCUA_CLIENT_KEY` | — | Private key for `OPCUA_CLIENT_CERT` |
| `OPCUA_APPLICATION_URI` | the `subjectAltName` URI of `OPCUA_CLIENT_CERT` | Application URI announced to the server. Set it only for a certificate that carries no URI of its own |
| `OPCUA_SERVER_CERT` | — | The OPC UA **server's** certificate, pinned. Without it, encryption protects against eavesdropping but not against an impostor endpoint. Requires a policy other than `None` |
| `OPCUA_USERNAME` | — | Username identity; the session is anonymous when unset |
| `OPCUA_PASSWORD` | — | Password for `OPCUA_USERNAME` |
| `OPCUA_USER_CERT` | — | Certificate identifying the **user**, for X.509 authentication. A different key pair from `OPCUA_CLIENT_CERT`, which secures the channel. Cannot be combined with `OPCUA_USERNAME` |
| `OPCUA_USER_KEY` | — | Private key for `OPCUA_USER_CERT`. Signs the server's challenge; never sent |
| `OPCUA_PROFILE` | `observe` | `observe`, `operator`, or `full` tool profile (`read-only` is an alias for `observe`) |
| `OPCUA_POLICY_FILE` | — | Optional version-1 JSON policy file; environment variables override it |
| `OPCUA_ALLOWED_TOOLS` | — | Comma-separated allowlist that can only narrow the selected profile |
| `OPCUA_ALLOWED_WRITE_NODES` | — | Comma-separated node IDs writable by the `operator` profile. `ns=2;i=5` or, preferably, `nsu=<namespace-uri>;i=5` — see [Writing an allowlist that stays correct](#writing-an-allowlist-that-stays-correct) |
| `OPCUA_ALLOWED_METHODS` | — | Comma-separated `object_node_id|method_node_id` pairs callable by `operator` |
| `OPCUA_ALLOW_ACKNOWLEDGE_ALARMS` | `false` | Allow `operator` to acknowledge alarms |
| `OPCUA_ALLOW_INSECURE_CONTROL` | `false` | Lab-only override permitting control tools without OPC UA channel security |
| `OPCUA_RECONNECT_INITIAL_DELAY_MS` | `1000` | Delay before the first reconnection attempt; doubles each attempt |
| `OPCUA_RECONNECT_MAX_DELAY_MS` | `8000` | Ceiling for that doubling |
| `OPCUA_RECONNECT_MAX_RETRY` | `3` | Retries after the first attempt. `0` disables retrying, `-1` retries forever |
| `OPCUA_SESSION_TIMEOUT_MS` | `60000` | Session timeout asked of the OPC UA server; also sets the keep-alive period |

### Writing an allowlist that stays correct

`OPCUA_ALLOWED_WRITE_NODES` and `OPCUA_ALLOWED_METHODS` accept two forms:

```
ns=2;i=5                       # namespace index — resolved per session
nsu=urn:plant:line-a;i=5       # namespace URI — stable across sessions
```

**Prefer the second.** A namespace *index* is not a property of a node; it is
that node's position in the server's NamespaceArray for the current session. A
firmware update, an added namespace or a reordered load can move it — and an
allowlist written `ns=2;i=5` then authorises writes to a **different physical
node**, with nothing anywhere reporting that anything changed.

The namespace URI is the stable name. Both servers read the NamespaceArray on
every connect and resolve URI-pinned entries against it, so the allowlist follows
the node rather than the index. An entry naming a URI the server does not
publish matches nothing and is reported on stderr at connect time.

Spelling no longer matters: `i=2253` and `ns=0;i=2253` are the same node, entries
are trimmed, and both runtimes canonicalise identically (pinned by
`tests/fixtures/node-id-forms.json`).

### Staying connected

Neither server needs restarting when the OPC UA server does. A dropped
connection is retried with exponential backoff on the four
`OPCUA_RECONNECT_*` / `OPCUA_SESSION_TIMEOUT_MS` settings above, the read and
write paths transparently re-establish a dead session, and the data-change
subscriptions an agent is holding are re-created on the new session — the IDs
keep working and the values already buffered are still there to be read.

Reconnection is driven by tool calls rather than by a timer: if the endpoint is
unreachable when the MCP client starts, the server still starts, and the first
call that needs a session connects. `get_server_status` is the one tool that
answers either way — it reports `connected: false` and the reason instead of
failing, and every other tool's error points at it.

The defaults (three retries, 1–8s apart) keep a single tool call from hanging for
long. Raise `OPCUA_RECONNECT_MAX_RETRY` for a site where outages are measured in
minutes; the last waiting a call will do is the sum of the delays.

The default `observe` profile advertises only read, browse, history and monitoring
tools. `operator` exposes only explicitly allowlisted write targets and methods;
an entire batch write is rejected before touching OPC UA if any target is outside
the allowlist. `full` exposes all tools. Both `operator` and `full` still require
a secured OPC UA channel unless `OPCUA_ALLOW_INSECURE_CONTROL=true` is set
explicitly. Policy is enforced again on every call, not only when tools are
listed, and configuration changes take effect after restarting the MCP process.

Example production policy (`OPCUA_POLICY_FILE=/etc/opcua-mcp-policy.json`):

```json
{
  "version": 1,
  "profile": "operator",
  "allowed_tools": ["read_opcua_node", "write_opcua_node", "call_opcua_method"],
  "control": {
    "writable_nodes": ["ns=2;s=Line1.SpeedSetpoint"],
    "callable_methods": [
      { "object_id": "ns=2;s=Line1", "method_id": "ns=2;s=Line1.Reset" }
    ],
    "acknowledge_alarms": false
  }
}
```

Encrypted, authenticated connection to a real server:

```json
{
  "mcpServers": {
    "opcua": {
      "command": "npx",
      "args": ["-y", "opcua-mcp-server"],
      "env": {
        "OPCUA_SERVER_URL": "opc.tcp://plc.example.internal:4840",
        "OPCUA_SECURITY_POLICY": "Basic256Sha256",
        "OPCUA_CLIENT_CERT": "/etc/opcua/client.pem",
        "OPCUA_CLIENT_KEY": "/etc/opcua/client_key.pem",
        "OPCUA_USERNAME": "mcp-operator",
        "OPCUA_PASSWORD": "…",
        "OPCUA_PROFILE": "operator",
        "OPCUA_POLICY_FILE": "/etc/opcua-mcp-policy.json"
      }
    }
  }
}
```

Names are case-insensitive, and a policy on its own implies `SignAndEncrypt`.
Anything the OPC UA spec cannot honour — a mode without a policy, a policy
without a certificate, a username without a password — is refused at startup
with a message naming the variable, rather than failing later against live
equipment. The server certificate is taken from the endpoint description during
the handshake, so no server certificate file is needed.

Generating a client certificate the server will accept, and getting it into its
trust list, is **[docs/certificates.md](docs/certificates.md)**.

`OPCUA_USERNAME` / `OPCUA_PASSWORD` authenticate the session but encrypt
nothing: without a security policy the password crosses the network in clear
text unless the server's user-token policy protects it, and both servers say so
on stderr. Pair credentials with a policy.

On the **Python runtime**, certificate and key files are parsed as PEM only when
they are named `*.pem` and as DER otherwise (a `python-opcua` rule), so a PEM key
called `client.key` fails to load — name it `client_key.pem`. The Node runtime
sniffs the contents and accepts either name.

## Installation

Most users need only the [Quick Start](#quick-start) above — `npx` and `uvx`
fetch the package on demand. To install it permanently:

```bash
# Node
npm install -g opcua-mcp-server
opcua-mcp-server            # also available as: opcua-mcp

# Python
uv tool install opcua-mcp-server   # or: pip install opcua-mcp-server
opcua-mcp-server
```

Either command, once installed, can register itself with Claude Desktop:

```bash
opcua-mcp-server --install claude-desktop --url opc.tcp://192.168.0.10:4840
```

That writes absolute paths rather than a bare `npx`, which matters more than it
sounds: Claude Desktop is launched from the GUI and does not inherit a login
shell's `PATH`. `--dry-run` shows the result without writing it. Downloadable
artifacts for machines with no runtime at all — the `.mcpb` bundle and the
single-file executables — are covered in **[docs/install.md](docs/install.md)**.

| | Python | Node |
|---|---|---|
| Requires | Python 3.10+ | Node 22.13+ |
| Package | [PyPI `opcua-mcp-server`](https://pypi.org/project/opcua-mcp-server/) | [npm `opcua-mcp-server`](https://www.npmjs.com/package/opcua-mcp-server) |
| Framework | `mcp` (`MCPServer`) | `@modelcontextprotocol/sdk` |
| OPC UA library | `opcua` (FreeOpcUa) | `node-opcua-client` |
| Source | `packages/server-python/` | `packages/server-node/` |

Exact dependency versions live in the manifests
([`pyproject.toml`](packages/server-python/pyproject.toml),
[`package.json`](packages/server-node/package.json)) rather than being restated
here, where they would drift.

## Try it against the mock

The repo ships a simulated industrial plant — sensors, actuators, methods,
history and events — so you can try the tools without touching real equipment.
Two smaller mocks cover what it deliberately does not model: server-side
aggregates (`packages/mock-server-aggregate`, port 4841) and Alarms & Conditions
with retained, acknowledgeable alarms (`packages/mock-server-alarms`, port 4842).
The [compatibility matrix](docs/compatibility.md) says which mock covers what.

```bash
git clone https://github.com/midhunxavier/OPCUA-MCP.git && cd OPCUA-MCP
uv sync --all-packages
uv run --no-sync opcua-mock-server     # listens on opc.tcp://localhost:4840/freeopcua/server/
```

Then point your MCP client at
`opc.tcp://localhost:4840/freeopcua/server/`. See
[`.mcp.json.example`](.mcp.json.example) for a ready-made config, and
[docs/testing.md](docs/testing.md) for an MCP Inspector walkthrough and example
prompts.

## Development & testing

```bash
uv sync --all-packages                      # one-time workspace setup
cd tests
uv run --no-sync pytest unit/               # fast, no server needed (<1s)
uv run --no-sync pytest                     # unit + end-to-end, both runtimes
uv run --no-sync pytest -m smoke smoke/     # published-artifact smoke tests
```

Full guide, including the MCP Inspector and AI-agent walkthroughs:
**[docs/testing.md](docs/testing.md)**. Project layout and how to add a tool:
**[CONTRIBUTING.md](CONTRIBUTING.md)**. How it fits together:
**[docs/architecture.md](docs/architecture.md)**.

## Security

> [!WARNING]
> Both runtimes default to an **observe-only** tool profile, but the OPC UA
> connection itself still defaults to `SecurityPolicy.None` and
> `MessageSecurityMode.None` — unauthenticated and unencrypted. That combination
> is for the bundled mock and local development. For production, configure both
> channel security and an `operator` allowlist as shown above. Control tools are
> blocked on an insecure channel unless the explicit lab override is set.

See [SECURITY.md](SECURITY.md) for the security posture, what the servers do and
do not verify, and how to report a vulnerability;
[docs/certificates.md](docs/certificates.md) for client certificates and trust
setup.

The MCP policy is defense in depth, not a replacement for OPC UA authorization.
Scope the OPC UA account to the same nodes and methods; use a separate read-only
account for `observe` deployments.

## Contributing

Contributions are welcome — see **[CONTRIBUTING.md](CONTRIBUTING.md)** for
project layout, local development, adding a new tool to both servers, and PR
conventions. What is planned next is in [ROADMAP.md](ROADMAP.md); changes that
have landed are tracked in [CHANGELOG.md](CHANGELOG.md).

Results from a real OPC UA server are the most useful thing to send: open a
[compatibility report](https://github.com/midhunxavier/OPCUA-MCP/issues/new?template=compatibility_report.md)
saying which server, which version and which tools worked. Test only on
equipment you are authorised to use, and keep writes and method calls to a
simulator or lab.

## License

MIT — see [LICENSE](LICENSE).
