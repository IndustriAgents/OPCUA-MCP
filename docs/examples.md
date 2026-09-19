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

Discover these any time with `browse_opcua_nodes`.

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
| SystemStatus / EmergencyStopCommand | `ns=2;i=25` | Boolean | write |
| SystemStatus / ResetSystemCommand | `ns=2;i=26` | Boolean | write |
| Methods folder | `ns=2;i=27` | Object | — |
| Methods / StartProduction | `ns=2;i=28` | Method | call (1 Double arg) |
| Methods / StopProduction | `ns=2;i=31` | Method | call |
| Methods / EmergencyStop | `ns=2;i=33` | Method | call |
| Methods / ResetSystem | `ns=2;i=35` | Method | call |
| Methods / CalibrateSensors | `ns=2;i=37` | Method | call (1 String arg) |
| Scratch / ScratchDouble | `ns=2;i=41` | Double | read/write |
| Scratch / ScratchBoolean | `ns=2;i=42` | Boolean | read/write |
| Scratch / ScratchAnalog | `ns=2;i=90` | Double, AnalogItemType | read/write |

> Method NodeIds account for the per-method `InputArguments`/`OutputArguments`
> property nodes. Always browse the `Methods` folder rather than hard-coding.

> The `Scratch` nodes are the writable ones the simulation never touches; every
> other writable node is an actuator the mock republishes from its own state once
> a second, so writing one and reading it back races a timer. `ScratchAnalog` is
> the one node that says what its number *means*: an `AnalogItemType` with
> `EngineeringUnits` (°C), `EURange` (0 to 150) and `InstrumentRange` (-50 to
> 250) — which makes it the node to try a write outside the range on.

---

## Core tools (both servers)

### `read_opcua_nodes`
Read one or more nodes. One call, one round trip, whether it is one node or fifty.
```json
{ "node_ids": ["ns=2;i=3", "ns=2;i=4", "ns=2;i=12"] }
```
```json
{ "node_id": "ns=2;i=3", "value": 26.13, "data_type": "Double", "status": "Good",
  "source_timestamp": "2026-09-10T13:15:12.214Z",
  "server_timestamp": "2026-09-10T13:15:12.214Z", "engineering": null }
{ "node_id": "ns=2;i=4", "value": 1010.57, "data_type": "Double", … }
{ "node_id": "ns=2;i=12", "value": false, "data_type": "Boolean", … }
```
The quality and the age come with the value, because they are what decide whether
it can be acted on: a `status` of `Good` and a `source_timestamp` from four hours
ago are a stale reading, and a bare number cannot tell you that.

`engineering` is what the plant says the number *means*, read from the node's own
OPC UA properties (Part 8 §5.3) and cached for the session. `null` for a node that
publishes none, which is most of them:

```json
{ "node_id": "ns=2;i=90", "value": 50.0, "data_type": "Double", "status": "Good",
  "engineering": { "unit": "°C", "unit_description": "degree Celsius",
                   "eu_range": { "low": 0, "high": 150 },
                   "instrument_range": { "low": -50, "high": 250 } }, … }
```

`eu_range` is what the value holds in normal operation, `instrument_range` what
the device can physically return. The first is not only reported — a write
outside it is refused before anything is sent.

A node the server rejects is one `Bad…` status among the others, never a failed
call — one unreadable node must not discard the other forty-nine.
> Prompt: *"Read temperature, pressure, and pump status together."*

### `write_opcua_nodes`
Write one or more nodes. **This changes physical equipment.**
```json
{ "nodes": [
  { "node_id": "ns=2;i=13", "value": 80 },
  { "node_id": "ns=2;i=24", "value": true }
] }
```
```json
{ "node_id": "ns=2;i=13", "status": "Good", "error": null }
{ "node_id": "ns=2;i=24", "status": "Good", "error": null }
```
Without `data_type` each node is read first to learn its type. Give it to skip
that round trip — and to write a **write-only** node, which refuses the read:
```json
{ "nodes": [{ "node_id": "ns=2;i=13", "value": 80, "data_type": "Double" }] }
```
`status` is what the OPC UA server answered; `error` is why this server never
sent the write at all (an unconvertible value, a type it could not read). The two
are separate because "the server refused" and "we never asked" are different
problems with different fixes.
> Note: the simulation republishes sensor/actuator state every ~1s, so direct
> writes to those nodes are transient. Use the **command variables** or
> **methods** to drive lasting state changes.
> Prompt: *"Open valve V-101 to 80%."*

### `browse_opcua_nodes`
Explore the address space: list children, walk a subtree, resolve a path by name,
or search. One tool for all four, because they are one traversal with different
bounds.

**List a node's children** (the default, `depth: 1`):
```json
{ "node_id": "ns=2;i=1" }
```
```json
{ "nodes": [
    { "node_id": "ns=2;i=2", "browse_name": "2:Sensors", "node_class": "Object",
      "parent_node_id": "ns=2;i=1", "data_type": null, "value": null,
      "description": null },
    { "node_id": "ns=2;i=11", "browse_name": "2:Actuators", … },
    { "node_id": "ns=2;i=27", "browse_name": "2:Methods", … } ],
  "truncated": false, "inspected": 4 }
```
> Prompt: *"What folders are under the Industrial Control System?"*

**Inventory every variable** (what the retired `get_all_variables` did):
```json
{ "depth": 4, "node_class": "Variable", "include_values": true }
```
```json
{ "nodes": [
    { "node_id": "ns=2;i=3", "browse_name": "2:Temperature", "node_class": "Variable",
      "parent_node_id": "ns=2;i=2", "data_type": "Double", "value": 26.5,
      "description": "Temperature" }, … ],
  "truncated": false, "inspected": 22 }
```
The built-in `Server` subtree is always skipped — several hundred nodes of the
server describing itself, identical everywhere, and `get_server_status` answers
what anyone would browse it for.
> Prompt: *"Give me a complete inventory of everything on this server."*

**Resolve a name to a node id** (`depth: 0` returns just the addressed node):
```json
{ "browse_path": "/Objects/IndustrialControlSystem/Sensors/Temperature", "depth": 0 }
```
```json
{ "nodes": [ { "node_id": "ns=2;i=3", "browse_name": "2:Temperature", … } ],
  "truncated": false, "inspected": 1 }
```
This is where to start when you know what a thing is *called* but not its numeric
id. A bare segment matches whatever namespace it is in; write `2:Sensors` to pin
one. A path that does not resolve is an error naming the segment that failed, not
an empty result.

**Search by name:**
```json
{ "depth": 4, "name_filter": "temp" }
```
> Prompt: *"Find me anything to do with temperature."*

**`truncated` is part of the answer.** Every walk is bounded by `max_nodes`
(default 500), and a walk that stopped early says so — a prefix of the address
space is otherwise indistinguishable from all of it.

### `call_opcua_method`
Call a method on an object node.
```json
{ "object_node_id": "ns=2;i=27", "method_node_id": "ns=2;i=28", "arguments": ["60"] }
```
```json
{ "object_node_id": "ns=2;i=27", "method_node_id": "ns=2;i=28",
  "status": "Good", "outputs": [true] }
```
Arguments are converted to the types the method *declares*: this server reads the
method's `InputArguments`, so a Boolean argument is sent as a Boolean and an
Int32 as an Int32, rather than everything becoming a Double or a String.
After this, `SystemMode` (`ns=2;i=19`) becomes `AUTO` and `ProductionRate`
(`ns=2;i=21`) becomes `60` within ~1s.
> Prompt: *"Start production at 60 units/hour, then stop it."*

### `get_server_status`
Connection state, server health, and the namespace array — the first thing to try
when another tool fails.
```json
{}
```
```json
{
  "connected": true,
  "endpoint_url": "opc.tcp://localhost:4840/freeopcua/server/",
  "security": "policy=None mode=None user=anonymous",
  "server_state": "Running",
  "current_time": "2026-09-17T13:06:37.580Z",
  "start_time": "2026-09-17T13:06:10.558Z",
  "build_info": {
    "product_name": "FreeOpcUa Python Server",
    "product_uri": "urn:freeopcua.github.io:python:server",
    "manufacturer_name": "FreeOpcUa",
    "software_version": "1.0pre",
    "build_number": "0",
    "build_date": "2026-09-17T13:06:10.558Z"
  },
  "namespaces": [
    { "index": 0, "uri": "http://opcfoundation.org/UA/" },
    { "index": 1, "uri": "urn:freeopcua:python:server" },
    { "index": 2, "uri": "http://examples.freeopcua.github.io" }
  ],
  "error": null
}
```
> Prompt: *"Are we actually connected, and is the PLC healthy?"*

Use `namespaces` rather than hard-coding a namespace index: the same URI can sit
at a different index after a server restart, so an `ns=2;i=3` that worked
yesterday may address something else today.

This is the one tool that never fails for being disconnected — it reports it:

```json
{
  "connected": false,
  "endpoint_url": "opc.tcp://localhost:4840",
  "security": "policy=None mode=None user=anonymous",
  "server_state": null,
  "current_time": null,
  "start_time": null,
  "build_info": null,
  "namespaces": [],
  "error": "connect ECONNREFUSED 127.0.0.1:4840"
}
```

Calling it is also what re-establishes a dropped connection, so it doubles as
"try again now". See [Staying connected](../README.md#staying-connected).

---

## History and aggregates

One tool, capability-gated. The mock server enables history and advertises no
aggregate functions, so against it `read_opcua_history` appears *without* its
`aggregate_function` argument.

### `read_opcua_history` (both servers)
Read a node's stored history — raw readings, or one server-computed summary per
interval. Offered when the server advertises historical access
(`AccessHistoryDataCapability`) *or* aggregates.

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

#### With an aggregate
Add `aggregate_function` and the server computes one summary value per
`processing_interval` (ms) instead of returning raw readings — which is how to
ask about a week of data without transferring a week of data.
```json
{ "node_id": "ns=2;i=3", "start_time": "2026-06-05T09:50:00Z",
  "aggregate_function": "Average", "processing_interval": 60000 }
```

**The argument only appears when the server advertises aggregates**, and its
description then lists the functions that server actually offers. The bundled
mock advertises none, so against it `read_opcua_history` is offered *without*
`aggregate_function` — capability gating applied to the argument rather than to
the whole tool, which is strictly more informative: you are told what this server
can do, not merely that a tool is missing.

The result uses the same record shape as the raw read above, one record per
interval:

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
long after `subscribe_opcua_nodes` has returned. So the MCP server **buffers**
them. You subscribe once, then read the accumulated values back whenever you
like — from `list_subscriptions` or from the `opcua://subscriptions` resource.

### `subscribe_opcua_nodes`
Start watching one or more nodes. Each gets its own subscription record and id.
```json
{ "node_ids": ["ns=2;i=3"], "publishing_interval": 500,
  "sampling_interval": 0, "buffer_size": 20 }
```
```json
{ "subscription_id": "sub-1", "node_id": "ns=2;i=3",
  "publishing_interval": 500, "sampling_interval": 500,
  "buffer_size": 20, "change_count": 0, "changes": [] }
```

| Argument | Default | Meaning |
|---|---|---|
| `node_ids` | — | The nodes to monitor. A single node is a one-element list. |
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
`read_opcua_history`, and each entry of `changes` is a `historyRecords`
record. `change_count` counts every change received; `changes` holds only the
newest `buffer_size` of them.

> Prompt: *"What has the temperature done since I asked you to watch it?"*

### `unsubscribe_opcua_nodes`
Cancel subscriptions and discard their buffers.
```json
{ "subscription_ids": ["sub-1"] }
```
```json
{ "subscription_id": "sub-1", "node_id": "ns=2;i=3",
  "publishing_interval": 500, "sampling_interval": 500,
  "buffer_size": 20, "change_count": 4, "changes": [ … ] }
```
The subscription is returned as it was at the moment it was cancelled, so
anything still buffered can be read one last time rather than being thrown away
with it.

Every id is checked before any is cancelled, so an ID that is not active cancels
nothing — a typo must not cost the buffers of the subscriptions named beside it:
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

## Events & alarms

Added for issue #4, and not capability-gated either: any OPC UA server has a
Server object that events are raised from, and one that raises none simply has
none to hand over. Alarms are the same machinery with a condition attached.

### `subscribe_events`
Start collecting events. Returns immediately — the subscription runs in the
background, because MCP has no way for the server to push one at you.
```json
{ "node_id": "ns=0;i=2253", "severity_min": 500, "buffer_size": 100 }
```
```
Subscribed to events from node ns=0;i=2253, buffering up to 100 events of
severity 500 or above. Read them with read_events.
```
Every argument is optional: the default notifier is the Server object
(`ns=0;i=2253`), where most servers raise everything they have. Subscribing to
the same node again restarts it with the new settings.
> Prompt: *"Watch for anything serious happening on the plant."*

### `read_events`
Hand over what has arrived, oldest first. The events returned are **removed**
from the buffer, so a second call returns only what is new.
```json
{ "node_id": "ns=0;i=2253", "limit": 50 }
```
One content block per event:
```json
{ "event_id": "ZDAzNzVmNzlhYzM0NDNjMWI3MzdhMmJhMmRmNzFiN2E=",
  "event_type": "ns=0;i=2041", "source_node": "ns=2;i=1",
  "source_name": "IndustrialControlSystem", "time": "2026-09-12T08:36:07.280Z",
  "message": "Alarm active: emergency stop", "severity": 700,
  "condition_id": null, "condition_name": null,
  "active": null, "acked": null, "retain": null }
```
Every field is present on every event; the condition fields are `null` for a
plain event like this one. If the buffer overflowed since the last read, one
last block — prose, not a record — says how many events were lost and what to
raise. The mock raises exactly this when its alarm state
changes — write `true` to `ns=2;i=25` to see it, and to `ns=2;i=26` to clear it.
> Prompt: *"Anything happen since we last looked?"*

### `list_active_alarms`
The alarms the server is retaining right now — active, unacknowledged, or both.
Needs no prior `subscribe_events`: it asks the server directly, with
ConditionRefresh.
```json
{ "node_id": "ns=0;i=2253", "timeout_seconds": 5 }
```
```json
{ "event_id": "ZjW7HJrVSFzDV2sMsX7sEQAAAAE=", "event_type": "ns=0;i=9341",
  "source_node": "ns=1;i=1001", "source_name": "Temperature",
  "time": "2026-09-12T08:34:21.872Z",
  "message": "Condition is 100.000 and state is High", "severity": 700,
  "condition_id": "ns=1;i=1002", "condition_name": "HighTemperatureAlarm",
  "active": true, "acked": false, "retain": true }
```
If the server does not finish within `timeout_seconds`, that is an error too,
not a short list: a partial answer cannot be told apart from "no alarms".

Against a server with no Alarms & Conditions support — the bundled mock included
— this says so rather than returning an empty list:
```
Error: Failed to list active alarms from node ns=0;i=2253: ConditionRefresh
failed with status: BadNothingToDo (0x800f0000). The server may not implement
OPC UA Alarms & Conditions.
```
To try the working path, run the alarms mock instead:
`cd packages/mock-server-alarms && npm install && npm start` (:4842).
> Prompt: *"What alarms are active right now?"*

### `acknowledge_alarm`
Acknowledge one, by the `event_id` that reported it. The condition behind that
event is remembered from the call that reported it, so it need not be repeated.
```json
{ "event_id": "ZjW7HJrVSFzDV2sMsX7sEQAAAAE=", "comment": "on it — checking the cooler" }
```
```
Acknowledged alarm ns=1;i=1002 (event ZjW7HJrVSFzDV2sMsX7sEQAAAAE=)
```
Acknowledging tells the server an operator has seen the alarm. It does not clear
the underlying condition: `active` stays `true` until the plant says otherwise.
Pass `condition_id` explicitly for an event that came from somewhere other than
this server's own `read_events` / `list_active_alarms`.
> Prompt: *"Acknowledge the high-temperature alarm, note that I'm on it."*

The record shape above is defined once, in `../contract/tools.json` under
`resultShapes.eventRecords`, with the OPC UA browse path behind each field in
the neighbouring `events.fields`. Both servers are held to it by
`../tests/e2e/test_events_e2e.py`.

---

## Tip

You don't call these tools by hand in normal use — you ask Claude. The JSON above
is what Claude sends under the hood. See `../tests/` for an automated suite that
exercises every tool against both servers.
