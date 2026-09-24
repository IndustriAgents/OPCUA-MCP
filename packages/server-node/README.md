# OPC UA MCP Server (Node)

A Node / TypeScript Model Context Protocol (MCP) server for OPC UA operations, runnable with `npx`. It lets an MCP client read and write nodes, browse an address space, call methods, watch nodes for changes, and work the alarm and history surfaces of an OPC UA server.

There is a [Python runtime](https://github.com/IndustriAgents/OPCUA-MCP/tree/main/packages/server-python) exposing the same tools from the same contract. Both are first-class: one test suite runs against both, and they are released together under one version. Pick whichever stack you already run; the few declared differences between them, and the known divergences still being fixed, are listed in [docs/compatibility.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/compatibility.md#runtime-differences).

## Features

- **Read nodes** — value, data type, status, timestamps, and the engineering unit and range the node publishes, so a `51.75` arrives labelled °C rather than bare
- **Write nodes** — with the value bounded by the node's own published `EURange` before anything is sent, on top of whatever your policy allows
- **Browse** — list children, walk a subtree, resolve a browse path or search by name, with each node's `HasTypeDefinition` so an alarm reads as an alarm rather than as `Object`
- **Call methods** on object nodes
- **Data-change subscriptions** — watch nodes instead of polling them, with an optional absolute or percent deadband so jitter is discarded at the server
- **Events and alarms** — collect events from a notifier node, list the alarms the server retains, and acknowledge, confirm, annotate or shelve them
- **History** — historical values, raw or summarised by a server-side aggregate, and events the server stored for a range that has already passed
- **Server health** — connection state, the namespace array, and the OPC UA server's own `ServerDiagnosticsSummary` counters
- **Tool profiles and an audit trail** — an observe-only default, an allowlist for control, and every control call recorded
- **Reconnection** — a dropped session is rebuilt with backoff and the subscriptions on it re-created

## Installation & Usage

### Using npx (Recommended)

You can run the server directly using npx without installing it globally:

```bash
npx opcua-mcp-server
```

### Global Installation

```bash
npm install -g opcua-mcp-server
opcua-mcp-server
```

### Registering with Claude Desktop

Rather than editing `claude_desktop_config.json` by hand, let the server write it:

```bash
opcua-mcp-server --install claude-desktop --url opc.tcp://192.168.0.10:4840
```

It merges into the existing config, backs the old one up, and records absolute
paths — Claude Desktop is launched from the GUI and does not inherit a login
shell's `PATH`, so a bare `"command": "npx"` often works in a terminal and fails
in the app. Add `--dry-run` to see the result first, `--force` to replace an
existing `opcua` entry.

There is also a **downloadable `.mcpb` bundle** for Claude Desktop and
**single-file executables** that need no Node at all — see
[docs/install.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/install.md).

### Local Development

```bash
git clone <repository>
cd packages/server-node
npm install
npm run build
npm start
```

## Tools

Fifteen tools, defined once in
**[contract/tools.json](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/tools.json)**
and shared with the Python runtime so the two cannot drift apart.

| Tool                      | What it does                                                                              |
| ------------------------- | ----------------------------------------------------------------------------------------- |
| `read_opcua_nodes`        | Read one or more nodes — value, data type, status, timestamps, engineering unit and range |
| `browse_opcua_nodes`      | List children, walk a subtree, resolve a browse path, search by name                      |
| `write_opcua_nodes`       | Write to one or more nodes                                                                |
| `call_opcua_method`       | Invoke a method on an object node                                                         |
| `get_server_status`       | Connection state, server health, the namespace array and the server's own diagnostics     |
| `subscribe_opcua_nodes`   | Watch nodes for data changes instead of polling them, with an optional deadband           |
| `list_subscriptions`      | The active subscriptions, each with its buffered changes                                  |
| `unsubscribe_opcua_nodes` | Cancel subscriptions                                                                      |
| `subscribe_events`        | Start collecting events from a notifier node                                              |
| `read_events`             | Read the events collected since the last read                                             |
| `list_active_alarms`      | The alarms the server is currently retaining                                              |
| `acknowledge_alarm`       | Acknowledge one of them, with a comment                                                   |
| `act_on_alarm`            | Confirm, annotate or shelve an alarm — the rest of the operator workflow                  |
| `read_opcua_history` †    | Historical values, raw or summarised by a server-side aggregate                           |
| `read_event_history` †    | Events the server stored, for a range that has already passed                             |

† **Capability-gated.** `read_opcua_history` appears only when the connected
server advertises historical access (`AccessHistoryDataCapability`) or aggregates
(a non-empty `AggregateFunctions` folder). `read_event_history` is gated
separately on `AccessHistoryEventsCapability` — keeping values and keeping events
are different features and a server commonly does one without the other. What a
server cannot do is not on the menu, rather than failing at call time.

**One tool per operation, not one per arity.** Reading one node and reading fifty
is the same request with a longer list, so it is one tool and one code path.

Full per-tool reference with inputs, outputs and a node-ID map:
**[docs/examples.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md)**.

## Resources

One resource, `opcua://subscriptions`: the active data-change subscriptions and the values each has buffered, as JSON. It is the same set of records `list_subscriptions` returns, re-readable without spending a tool call. See [Subscriptions](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md#data-change-subscriptions) for the shape and the worked example.

## Configuration

The server is configured entirely through environment variables. Every one is
declared, with its type, default and secrecy, in the repository's
[`contract/config.json`](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/config.json),
which both runtimes are tested against and which ships inside this package:

| Variable                                | Default                                                 | Meaning                                                                                                                                                                                                                                            |
| --------------------------------------- | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `OPCUA_SERVER_URL`                      | `opc.tcp://localhost:4840`                              | OPC UA endpoint to connect to                                                                                                                                                                                                                      |
| `OPCUA_SECURITY_POLICY`                 | `None`                                                  | `None`, `Basic128Rsa15`, `Basic256`, `Basic256Sha256`, `Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss`                                                                                                                                             |
| `OPCUA_SECURITY_MODE`                   | `SignAndEncrypt` once a policy is set, otherwise `None` | `None`, `Sign` or `SignAndEncrypt`                                                                                                                                                                                                                 |
| `OPCUA_CLIENT_CERT`                     | —                                                       | Client certificate (PEM/DER). Required for any policy other than `None`                                                                                                                                                                            |
| `OPCUA_CLIENT_KEY`                      | —                                                       | Private key for `OPCUA_CLIENT_CERT`                                                                                                                                                                                                                |
| `OPCUA_APPLICATION_URI`                 | the `subjectAltName` URI of `OPCUA_CLIENT_CERT`         | Application URI announced to the server. Set it only for a certificate that carries no URI of its own                                                                                                                                              |
| `OPCUA_SERVER_CERT`                     | —                                                       | The OPC UA **server's** certificate, pinned. Without it, encryption protects against eavesdropping but not against an impostor endpoint, and **control tools are refused**. Requires a policy other than `None`; an expired pin refuses to connect |
| `OPCUA_USERNAME`                        | —                                                       | Username identity; the session is anonymous when unset                                                                                                                                                                                             |
| `OPCUA_PASSWORD`                        | —                                                       | Password for `OPCUA_USERNAME`                                                                                                                                                                                                                      |
| `OPCUA_USER_CERT`                       | —                                                       | Certificate identifying the **user**, for X.509 authentication. A different key pair from `OPCUA_CLIENT_CERT`, which secures the channel. Cannot be combined with `OPCUA_USERNAME`                                                                 |
| `OPCUA_USER_KEY`                        | —                                                       | Private key for `OPCUA_USER_CERT`. Signs the server's challenge; never sent                                                                                                                                                                        |
| `OPCUA_PROFILE`                         | `observe`                                               | `observe`, `operator`, or `full` tool profile (`read-only` is an alias for `observe`)                                                                                                                                                              |
| `OPCUA_POLICY_FILE`                     | —                                                       | Optional version-1 JSON policy file; environment variables override it                                                                                                                                                                             |
| `OPCUA_ALLOWED_TOOLS`                   | —                                                       | Comma-separated allowlist that can only narrow the selected profile                                                                                                                                                                                |
| `OPCUA_ALLOWED_WRITE_NODES`             | —                                                       | Comma-separated node IDs writable by the `operator` profile. `ns=2;i=5` or, preferably, `nsu=<namespace-uri>;i=5`                                                                                                                                  |
| `OPCUA_ALLOWED_METHODS`                 | —                                                       | Comma-separated `object_node_id\|method_node_id` pairs callable by `operator`                                                                                                                                                                      |
| `OPCUA_ALLOW_ACKNOWLEDGE_ALARMS`        | `false`                                                 | Allow `operator` to act on alarms — `acknowledge_alarm` and every `act_on_alarm` action                                                                                                                                                            |
| `OPCUA_ALLOW_INSECURE_CONTROL`          | `false`                                                 | Lab-only override permitting control tools over a channel with **no** security (`SecurityPolicy=None`). Does not cover an unverified server on a secured one                                                                                       |
| `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL` | `false`                                                 | Lab-only override permitting control tools over a secured channel whose server certificate is **not pinned** — encrypted, but to whoever answered                                                                                                  |
| `OPCUA_ALLOW_OUT_OF_RANGE_WRITES`       | `false`                                                 | Allow a write outside the `EURange` the OPC UA server itself published for that node                                                                                                                                                               |
| `OPCUA_AUDIT_FILE`                      | —                                                       | Append-only file for the control audit trail, one JSON object per line, written _beside_ stderr. A file that cannot be opened stops the server rather than falling back                                                                            |
| `OPCUA_OPERATOR_ID`                     | —                                                       | Label stamped on every audit record, so a shipped log says which deployment a control call came from                                                                                                                                               |
| `OPCUA_RECONNECT_INITIAL_DELAY_MS`      | `1000`                                                  | Delay before the first reconnection attempt; doubles each attempt                                                                                                                                                                                  |
| `OPCUA_RECONNECT_MAX_DELAY_MS`          | `8000`                                                  | Ceiling for that doubling                                                                                                                                                                                                                          |
| `OPCUA_RECONNECT_MAX_RETRY`             | `3`                                                     | Retries after the first attempt. `0` disables retrying, `-1` retries forever                                                                                                                                                                       |
| `OPCUA_SESSION_TIMEOUT_MS`              | `60000`                                                 | Session timeout asked of the OPC UA server; also sets the keep-alive period                                                                                                                                                                        |

Names are case-insensitive, and a policy on its own implies `SignAndEncrypt`.
An unusable combination — a mode without a policy, a policy without a
certificate, a username without a password — is refused at startup with a
message naming the variable. With no security configured the connection is
unencrypted and unauthenticated, and the server says so on stderr; see
[SECURITY.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/SECURITY.md).
Making a client certificate and getting it trusted:
[docs/certificates.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/certificates.md).

Examples:

```bash
# unsecured, e.g. against the bundled mock
OPCUA_SERVER_URL=opc.tcp://192.168.1.100:4840 npx opcua-mcp-server

# encrypted and authenticated
OPCUA_SERVER_URL=opc.tcp://plc.example.internal:4840 \
OPCUA_SECURITY_POLICY=Basic256Sha256 \
OPCUA_CLIENT_CERT=/etc/opcua/client.pem \
OPCUA_CLIENT_KEY=/etc/opcua/client_key.pem \
OPCUA_SERVER_CERT=/etc/opcua/plc_server.pem \
OPCUA_USERNAME=mcp-operator OPCUA_PASSWORD=… \
  npx opcua-mcp-server
```

### Deciding what the agent may do

Three profiles, and the default is the restrictive one:

| `OPCUA_PROFILE`       | What it offers                                                       |
| --------------------- | -------------------------------------------------------------------- |
| `observe` _(default)_ | Read, browse, history and monitoring. No writes, no methods          |
| `operator`            | The above, plus **only** the write targets and methods you allowlist |
| `full`                | Every tool                                                           |

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
`OPCUA_AUDIT_FILE` set, to an append-only JSON-lines file beside it.

### Staying connected

The connection is re-established by itself: a dropped or refused session is
retried with exponential backoff, and the read and write paths rebuild a dead
session rather than failing until the process is restarted. Data-change
subscriptions are re-created on the new session — the IDs keep working and the
values already buffered are still there to be read. Tune it with
`OPCUA_RECONNECT_INITIAL_DELAY_MS`, `OPCUA_RECONNECT_MAX_DELAY_MS`,
`OPCUA_RECONNECT_MAX_RETRY` and `OPCUA_SESSION_TIMEOUT_MS` above.

`get_server_status` reports whether the connection is up and what the OPC UA
server says about itself; it is the one tool that answers while the connection is
down, and calling it is also what brings a dropped one back. See
[Staying connected](https://github.com/IndustriAgents/OPCUA-MCP#staying-connected).

A control call whose session died before its response arrived is **not** silently
re-sent. OPC UA lets a Write partially succeed and defines no operation order, so
a dead session never proves the write did not land: the server rebuilds the
connection and then says plainly that the request may or may not have reached the
plant, naming what it was aimed at.

## Integration with Cursor/Claude

This server can be integrated with Cursor IDE or Claude Desktop for OPC UA operations. Add the following to your MCP configuration:

### Cursor Configuration

Add to your Cursor settings:

```json
{
  "mcpServers": {
    "opcua-node": {
      "command": "npx",
      "args": ["opcua-mcp-server"],
      "env": {
        "OPCUA_SERVER_URL": "opc.tcp://localhost:4840"
      }
    }
  }
}
```

### Claude Desktop Configuration

Add to your Claude Desktop configuration file:

```json
{
  "mcpServers": {
    "opcua-node": {
      "command": "npx",
      "args": ["opcua-mcp-server"],
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
- "Show me a complete inventory of all sensors and actuators"

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

User: "Show me all available variables in the system"
Assistant: I'll browse the OPC UA server's address space.
Tool call: browse_opcua_nodes
Result: "Found 15 variables:
- Name: Temperature_Sensor_01, NodeID: ns=2;i=101, Type: AnalogItemType, Value: 87.3 °C
- Name: Pressure_Sensor_01, NodeID: ns=2;i=102, Type: AnalogItemType, Value: 2.5 bar
- Name: Flow_Rate_01, NodeID: ns=2;i=103, Type: AnalogItemType, Value: 125.8 L/min
..."
```

## Security Considerations

- The connection defaults to no security (`SecurityPolicy.None`); set
  `OPCUA_SECURITY_POLICY` and credentials as shown under
  [Configuration](#configuration) for anything beyond local development
- The tool profile defaults to **observe-only**. Writes and methods require
  `OPCUA_PROFILE=operator` _and_ an explicit allowlist, over a channel to a
  verified server
- Pin the endpoint with `OPCUA_SERVER_CERT`. Without it the server certificate is
  taken from the endpoint description and not checked against anything, so
  encryption protects against eavesdropping but not against an impostor endpoint
  — and control tools are refused unless `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL`
  says otherwise, for a lab
- Ensure proper network security when connecting to industrial OPC UA servers
- Scope the OPC UA account you connect with to what the assistant should be able
  to do — the MCP policy is defense in depth, not a replacement for OPC UA
  authorization

See [SECURITY.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/SECURITY.md)
for the full posture and what the servers do and do not verify.

## Error Handling

The server provides detailed error messages for:

- Connection failures, distinguishing a refusal from an outage mid-request
- Invalid node IDs, and arguments a schema rejects — including misspelled
  optional ones, which are refused rather than silently ignored
- Type conversion errors
- Values outside a node's published range or a policy's bounds
- Method call failures
- Read/write operation errors

## Dependencies

- `@modelcontextprotocol/sdk`: MCP SDK for Node.js
- `node-opcua-client`: OPC UA client library for Node.js (the client half of `node-opcua`; the server half is not needed here and is deliberately not a dependency)

## Contributing

We welcome contributions to improve the OPC UA MCP Server!

**Repository**: [https://github.com/IndustriAgents/OPCUA-MCP](https://github.com/IndustriAgents/OPCUA-MCP)

To contribute:

1. Fork the repository at [https://github.com/IndustriAgents/OPCUA-MCP](https://github.com/IndustriAgents/OPCUA-MCP)
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Add tests if applicable
5. Commit your changes (`git commit -m 'Add some amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

Please feel free to open issues for bug reports, feature requests, or questions.

## License

MIT License - see LICENSE file for details

## Support

For issues and questions:

- Open an issue on [GitHub](https://github.com/IndustriAgents/OPCUA-MCP/issues)
- Check the OPC UA server connectivity with `get_server_status`
- Verify node IDs are correct
- Ensure proper permissions for OPC UA operations
- Review server logs for detailed error information
