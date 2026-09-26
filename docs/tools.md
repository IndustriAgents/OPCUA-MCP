# Tools

What the server offers an agent, what one call may ask for, and what comes back.
Per-tool inputs and outputs, with worked calls against the mock plant, are in
**[examples.md](examples.md)**. Which profile offers which tool is set in
**[configuration.md](configuration.md#deciding-what-the-agent-may-do)**.

<!-- BEGIN GENERATED: tool-reference from contract/tools.json by packages/server-node/scripts/config-artifacts.mjs. Do not edit by hand: edit the source, then run `npm run config:generate` in packages/server-node. -->

Both runtimes expose the same **15 tools** — 7 read, 4 monitor, 2 alarm-action and 2 control — defined once in [`contract/tools.json`](../contract/tools.json).

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
Batching is the caller's choice, not a different API.

Both servers also expose one **resource**, `opcua://subscriptions`: the same
records `list_subscriptions` returns, re-readable without spending a tool call.

## What comes back

Every answer comes back as a record, not prose. A reading carries its data type,
its OPC UA status and both timestamps — because quality and age are what decide
whether a value can be acted on, and a bare number carries neither:

```
read_opcua_nodes  node_ids=["ns=2;i=3", "ns=2;i=12"]
→ { "node_id": "ns=2;i=3", "value": 23.10, "data_type": "Double", "status": "Good",
    "source_timestamp": "2026-09-10T13:15:12.214Z",
    "server_timestamp": "2026-09-10T13:15:12.214Z",
    "engineering": { "unit": "°C", "unit_description": "degree Celsius",
                     "eu_range": { "low": 0, "high": 150 },
                     "instrument_range": { "low": -50, "high": 250 } } }
  { "node_id": "ns=2;i=12", "value": true, "data_type": "Boolean", "engineering": null, … }
```

`engineering` is what the plant says the number *means*, read from the node's own
OPC UA properties: `23.10` cannot be told from °C, PSI or %, and cannot be told
from a trip. It is `null` for a node that publishes none, which is most of them —
only an `AnalogItemType` carries it. The range is not only reported: a write
outside the node's own `EURange` is refused before anything is sent, which is a
safety bound the equipment declared rather than one a human retyped.

A walk of the address space says whether it finished, so a partial answer can
never pass for a complete one — in the record, and in the `completeness` object
every partial-capable tool returns beside it:

```
browse_opcua_nodes  depth=4  node_class="Variable"  include_values=true
→ { "nodes": [ { "node_id": "ns=2;i=3", "browse_name": "2:Temperature",
                 "node_class": "Variable", "data_type": "Double", "value": 26.34, … } ],
    "truncated": false, "inspected": 22 }
  completeness: { "complete": true, "reasons": [], … }
```

**Every tool declares a result shape**, and both runtimes are held to it. The
shapes live in `contract/tools.json`; the suite checks each runtime's *actual*
output against them and then diffs the two runtimes against each other — so a
client that has learned one server's answers can read the other's.

A per-node rejection is a status inside a successful result, never a failed call:
one unreadable node in a batch of fifty must not discard the other forty-nine.
Only a failure of the whole operation is an error.

Events are collected, not pushed: MCP is request/response, so `subscribe_events`
starts a real OPC UA subscription in the background and `read_events` hands over
what has arrived since you last asked. `list_active_alarms` does not need one —
it asks the server for its retained conditions directly (ConditionRefresh).

## How much one call may ask for

Every request is bounded before anything reaches the OPC UA server, and a request
over a bound is refused whole with a message naming the limit and the value —
never truncated, never partly sent. The numbers live in `contract/tools.json` ->
`limits`, so both runtimes enforce the same ones.

| Limit | Value | Bounds |
|---|---|---|
| `maxNodesPerRead` | 500 | `node_ids` of one `read_opcua_nodes` call (also the schema's `maxItems`) |
| `maxNodesPerWrite` | 100 | `nodes` of one `write_opcua_nodes` call (also `maxItems`) |
| `maxMethodArguments` | 64 | `arguments` of one `call_opcua_method` call (also `maxItems`) |
| `maxHistoryValues` | 5000 | Raw readings or stored events per history call, and intervals per aggregate read |
| `maxSubscriptions` | 200 | Data-change subscriptions held at once |
| `maxEventBufferSize` | 10000 | `subscribe_events` `buffer_size` — clamped, and reported as clamped |
| `maxRequestBytes` | 1 MiB | A call's arguments, as compact UTF-8 JSON |
| `maxStringBytes` | 128 KiB | Any one string, anywhere in the arguments |
| `maxByteStringBytes` | 64 KiB | Any one ByteString value, once its base64 is decoded |
| `maxArrayItems` | 10000 | Any one array, anywhere in the arguments |
| `maxNestingDepth` | 8 | How deeply arrays and objects nest, counting the arguments object |

The OPC UA server's own `OperationLimits` can only lower these. Reads are sent in
chunks of the server's `MaxNodesPerRead`, in order, each node keeping its own
status. A write batch over the server's `MaxNodesPerWrite` is **refused rather than
split**: a batch is one Write, OPC UA already lets one Write partially succeed
without rolling anything back, and splitting it would add a case where the first
part moved the plant and the second never arrived. A write is not a transaction
either way — read the per-node statuses.

## Results that say whether they are whole

A result that can hold fewer records than the request covered — a history read at
its `num_values`, a browse at `max_nodes`, an event buffer that overflowed, a
subscription whose ring buffer discarded old changes — carries a `completeness`
object beside `result` in `structuredContent`:

```
{ "complete": false, "reasons": ["requestLimit"], "returned": 2, "truncated": true,
  "limit": 2, "dropped": 0, "remaining": true,
  "continuation": { "start_time": "2026-09-24T10:00:01.123Z" } }
```

**Test `complete`; never infer completeness from how many records came back.**
`reasons` says why not (`requestLimit`, `contractLimit`, `serverLimit`,
`bufferOverflow`, `unbrowsable`), `dropped` counts records a buffer lost before the
read, and `continuation` is arguments to merge into the same call to get the rest
— stateless, so it cannot go stale or outlive a session. The tools that carry it
are `read_opcua_history`, `read_event_history`, `read_events`,
`browse_opcua_nodes` and the three subscription tools; the shape is
`contract/tools.json` -> `completeness`.
