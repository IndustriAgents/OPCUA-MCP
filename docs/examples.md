# OPC UA MCP — Tool Usage Examples

Concrete, tested examples for every tool exposed by the Python and Node MCP
servers, driven against the mock **Industrial Control System** OPC UA server.
Outputs below are real (abbreviated) responses captured end-to-end.

## Quick start

```bash
# 0) One-time: set up the Python workspace (from the repo root)
uv sync --all-packages

# 1) Start the mock OPC UA server (the simulated PLC/sensors)
uv run --no-sync opcua-mock-server        # listens on opc.tcp://localhost:4840/freeopcua/server/

# 2a) Python MCP server
OPCUA_SERVER_URL=opc.tcp://localhost:4840/freeopcua/server/ uv run --no-sync opcua-mcp-server

# 2b) Node MCP server
cd packages/server-node && npm install && npm run build
OPCUA_SERVER_URL=opc.tcp://localhost:4840/freeopcua/server/ node build/index.js
```

Both read the endpoint from `OPCUA_SERVER_URL` (default `opc.tcp://localhost:4840`).

## Node ID reference (mock server)

Discover these any time with `get_all_variables` or `browse_opcua_node_children`.

| Node | NodeId | Type | Access |
|------|--------|------|--------|
| Sensors / Temperature | `ns=2;i=3` | Double | read |
| Sensors / Pressure | `ns=2;i=4` | Double | read |
| Sensors / FlowRate | `ns=2;i=5` | Double | read |
| Sensors / MotorSpeed | `ns=2;i=10` | Double | read |
| Actuators / PumpEnabled | `ns=2;i=12` | Boolean | read/write |
| Actuators / ValvePosition | `ns=2;i=13` | Double | read/write |
| Actuators / HeaterPower | `ns=2;i=14` | Double | read/write |
| SystemStatus / SystemMode | `ns=2;i=19` | String | read/write |
| SystemStatus / ProductionRate | `ns=2;i=21` | Double | read |
| SystemStatus / StartProductionCommand | `ns=2;i=23` | Double | write |
| SystemStatus / StopProductionCommand | `ns=2;i=24` | Boolean | write |
| Methods folder | `ns=2;i=27` | Object | — |
| Methods / StartProduction | `ns=2;i=28` | Method | call (1 Double arg) |
| Methods / StopProduction | `ns=2;i=31` | Method | call |
| Methods / EmergencyStop | `ns=2;i=33` | Method | call |
| Methods / ResetSystem | `ns=2;i=35` | Method | call |
| Methods / CalibrateSensors | `ns=2;i=37` | Method | call (1 String arg) |

> Method NodeIds account for the per-method `InputArguments`/`OutputArguments`
> property nodes. Always browse the `Methods` folder rather than hard-coding.

---

## Core tools (both servers)

### `read_opcua_node`
Read one node's value.
```json
{ "node_id": "ns=2;i=3" }
```
```
Node ns=2;i=3 value: 26.94
```
> Prompt: *"What is the current temperature?"*

### `read_multiple_opcua_nodes`
Batch read.
```json
{ "node_ids": ["ns=2;i=3", "ns=2;i=4", "ns=2;i=12"] }
```
```json
{ "ns=2;i=3": 26.13, "ns=2;i=4": 1010.57, "ns=2;i=12": false }
```
> Prompt: *"Read temperature, pressure, and pump status together."*

### `write_opcua_node`
Write one node; the value is coerced to the node's data type.
```json
{ "node_id": "ns=2;i=13", "value": "80" }      // Double actuator
{ "node_id": "ns=2;i=24", "value": "true" }     // Boolean command
```
```
Successfully wrote 80 to node ns=2;i=13
```
> Note: the simulation republishes sensor/actuator state every ~1s, so direct
> writes to those nodes are transient. Use the **command variables** or
> **methods** to drive lasting state changes.
> Prompt: *"Open valve V-101 to 80%."*

### `write_multiple_opcua_nodes`
Batch write.
```json
{ "nodes_to_write": [
  { "node_id": "ns=2;i=13", "value": "80" },
  { "node_id": "ns=2;i=14", "value": "30" }
] }
```
```json
[ { "node_id": "ns=2;i=13", "status": "Success" },
  { "node_id": "ns=2;i=14", "status": "Success" } ]
```

### `browse_opcua_node_children`
List a node's children.
```json
{ "node_id": "ns=2;i=1" }
```
```json
[ { "node_id": "ns=2;i=2",  "browse_name": "2:Sensors" },
  { "node_id": "ns=2;i=11", "browse_name": "2:Actuators" },
  { "node_id": "ns=2;i=18", "browse_name": "2:SystemStatus" },
  { "node_id": "ns=2;i=27", "browse_name": "2:Methods" } ]
```
> Prompt: *"What folders are under the Industrial Control System?"*

### `call_opcua_method`
Call a method on an object node.
```json
{ "object_node_id": "ns=2;i=27", "method_node_id": "ns=2;i=28", "arguments": ["60"] }
```
```
Method call successful. Object: ns=2;i=27, Method: ns=2;i=28, Result: True
```
After this, `SystemMode` (`ns=2;i=19`) becomes `AUTO` and `ProductionRate`
(`ns=2;i=21`) becomes `60` within ~1s.
> Prompt: *"Start production at 60 units/hour, then stop it."*

### `get_all_variables`
Discover the whole address space (excludes the built-in `Server` subtree).
```json
{}
```
```
Found 22 variables:
- Name: Temperature   NodeID: ns=2;i=3   Value: 26.5   Data Type: ns=0;i=11
- Name: PumpEnabled   NodeID: ns=2;i=12  Value: false  Data Type: ns=0;i=1
...
```
> Prompt: *"Give me a complete inventory of everything on this server."*

---

## History & aggregate tools (added in PR #1)

These are exposed **only when the server advertises the capability**. The mock
server enables history (so the history tool appears) but advertises no aggregate
functions (so the aggregate tool stays hidden).

### `read_history_opcua_node` (both servers)
Read recorded historical values for a node. Exposed only when the server
advertises `AccessHistoryDataCapability`.

```json
{ "node_id": "ns=2;i=3", "start_time": "2026-06-05T09:50:00Z",
  "end_time": "2026-06-05T10:30:00Z", "num_values": 3 }
```

Both servers answer with the same records — one per historical value, returned
as one content block each:

```json
{ "value": 26.01, "timestamp": "2026-06-05T09:55:03.382Z", "status": "Good" }
```

| Field | Meaning |
|---|---|
| `value` | The recorded value, encoded per its OPC UA data type — a number stays a number, a ByteString is base64, a DateTime is ISO-8601 UTC, a NodeId is `ns=2;i=3`. `null` for an interval with no data. The full table is `../tests/fixtures/value-encoding.json`. |
| `timestamp` | Source timestamp, ISO-8601 UTC — the same format `start_time`/`end_time` accept. |
| `status` | OPC UA status code name, e.g. `Good`, `BadNoData`. |

The shape is defined once, in `../contract/tools.json` under
`resultShapes.historyRecords`, and both servers are held to it by
`../tests/e2e/test_contract_parity.py`. Earlier versions of the Node server
returned raw `DataValue` JSON here instead
(`{"statusCode": {"value": 0}, "sourceTimestamp": …}`); see `../CHANGELOG.md`.

> Prompt: *"Show the last 5 temperature readings from history."*

### `read_aggregate_opcua_node`
Computes aggregates (Average, Minimum, Maximum, …) over a time range, one value
per `processing_interval` (ms). **Requires a server that advertises aggregate
functions** — the bundled mock does not, so this tool is not exposed against it.
```json
{ "node_id": "ns=2;i=3", "start_time": "2026-06-05T09:50:00Z",
  "aggregate_function": "Average", "processing_interval": 60000 }
```

The result uses the same record shape as `read_history_opcua_node` above, one
record per interval:

```json
{ "value": 25.83, "timestamp": "2026-06-05T09:50:00.000Z", "status": "Good" }
```

> Prompt: *"What was the average temperature per minute over the last hour?"*

---

## Data-change subscriptions

Added for issue #3. Available on both servers, against any OPC UA server —
unlike history and aggregates, these are not capability-gated.

An MCP tool call is request/response, so a subscription cannot call the agent
back: the OPC UA notifications arrive whenever the server decides to publish,
long after `subscribe_opcua_node` has returned. So the MCP server **buffers**
them. You subscribe once, then read the accumulated values back whenever you
like — from `list_subscriptions` or from the `opcua://subscriptions` resource.

### `subscribe_opcua_node`
Start watching a node.
```json
{ "node_id": "ns=2;i=3", "publishing_interval": 500,
  "sampling_interval": 0, "buffer_size": 20 }
```
```json
{ "subscription_id": "sub-1", "node_id": "ns=2;i=3",
  "publishing_interval": 500, "sampling_interval": 500,
  "buffer_size": 20, "change_count": 0, "changes": [] }
```

| Argument | Default | Meaning |
|---|---|---|
| `node_id` | — | The node to monitor. |
| `publishing_interval` | `1000` | How often (ms) the OPC UA server publishes queued changes. Clamped to at least 50. |
| `sampling_interval` | `0` | How often (ms) it samples the node. `0` means "sample at `publishing_interval`", and the record reports the rate actually in force. A shorter interval queues several readings per publish. |
| `buffer_size` | `20` | How many of the most recent changes to retain. Clamped to 1..1000; older changes are discarded. |

> An OPC UA server sends the node's **current value** as the first change, so
> `change_count` reaches 1 without the value having moved.
> Prompt: *"Watch the temperature sensor."*

### `list_subscriptions`
Every active subscription and what it has collected since.
```json
{}
```
```json
{ "subscription_id": "sub-1", "node_id": "ns=2;i=3",
  "publishing_interval": 500, "sampling_interval": 500,
  "buffer_size": 20, "change_count": 4,
  "changes": [
    { "value": 25.33, "timestamp": "2026-09-12T08:24:11.478Z", "status": "Good" },
    { "value": 26.05, "timestamp": "2026-09-12T08:24:12.481Z", "status": "Good" },
    { "value": 24.23, "timestamp": "2026-09-12T08:24:13.484Z", "status": "Good" },
    { "value": 24.75, "timestamp": "2026-09-12T08:24:14.486Z", "status": "Good" } ] }
```

One record per subscription, one content block each — the same framing as
`read_history_opcua_node`, and each entry of `changes` is a `historyRecords`
record. `change_count` counts every change received; `changes` holds only the
newest `buffer_size` of them.

> Prompt: *"What has the temperature done since I asked you to watch it?"*

### `unsubscribe_opcua_node`
Cancel one subscription and discard its buffer.
```json
{ "subscription_id": "sub-1" }
```
```
Unsubscribed sub-1 from node ns=2;i=3 after 4 value changes
```
An ID that is not active is refused identically by both servers:
```
Error: No such subscription: sub-9
```

> Subscriptions do not outlive the MCP session. Both servers tear every one of
> them down before closing the OPC UA session, so a client that reconnects
> starts from none.

### Resource: `opcua://subscriptions`
The same records, re-readable without spending a tool call. `mimeType` is
`application/json`, and the document has one key:
```json
{ "subscriptions": [
  { "subscription_id": "sub-1", "node_id": "ns=2;i=3", "publishing_interval": 500,
    "sampling_interval": 500, "buffer_size": 20, "change_count": 4,
    "changes": [ { "value": 25.33, "timestamp": "2026-09-12T08:24:11.478Z", "status": "Good" } ] } ] }
```

Neither server sends `notifications/resources/updated`, and neither advertises
`resources.subscribe` — the agent re-reads. See
[architecture.md](architecture.md#why-the-subscriptions-resource-is-polled-not-pushed)
for why.

---

## Tip

You don't call these tools by hand in normal use — you ask Claude. The JSON above
is what Claude sends under the hood. See `../tests/` for an automated suite that
exercises every tool against both servers.
