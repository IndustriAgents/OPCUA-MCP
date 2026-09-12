# Architecture

How the pieces fit together, and the three invariants that are easy to break.

```mermaid
flowchart LR
    A["MCP client<br/>(Claude Desktop / Code / Cursor)"]
    B["opcua-mcp-server<br/>Python or Node"]
    C["OPC UA server<br/>(PLC / SCADA / mock)"]
    A -->|JSON-RPC over stdio| B
    B -->|OPC UA binary / TCP| C
```

## Two runtimes, one tool surface

The repo ships the same MCP server twice — once in Python, once in
TypeScript/Node. They are interchangeable: same tool names, same descriptions,
same parameters, same error wording. Users pick whichever runtime their stack
already has.

That interchangeability is not maintained by discipline. It is maintained by
`contract/tools.json`, the single source of truth for the tool surface:

| | How it uses the contract |
|---|---|
| **Node** | Builds its `tools/list` response directly from it. `npm run build` copies it to `build/contract.json` so the npm package is self-contained. |
| **Python** | Reads tool descriptions and capability node IDs from it. Input schemas are derived by `MCPServer` from the function signatures, and checked against the contract by a test. |

The contract also pins what the tools *return*, where the answer is more than
free text. A tool names a shape from `resultShapes`; the history family
(`read_history_opcua_node`, `read_aggregate_opcua_node`) shares
`historyRecords`, one flat `{value, timestamp, status}` record per historical
value or aggregate interval, and the event family (`read_events`,
`list_active_alarms`) shares `eventRecords`. Encoding a value keys on the OPC UA
data type rather than the language one — python-opcua and node-opcua represent
the same reading with entirely different native types (a ByteString is `bytes`
vs a `Buffer`, an Int64 a plain int vs a `[high, low]` pair), so anything
reaching for the runtime type diverges by construction. `tests/fixtures/value-encoding.json`
is the shared table, and both unit suites build the native value for every case
in it and assert the same JSON comes out. That was the second half of interchangeability, and
for a while it was missing: both servers matched on names and parameters but the
Node one returned raw `node-opcua` `DataValue` JSON while the Python one returned
flat records, so a client that learned one misread the other.

`tests/e2e/test_contract_parity.py` starts both servers and asserts each
advertises exactly the contract's applicable tools, with matching descriptions
and parameter sets, and that what each actually returns satisfies the declared
`resultShape`. `tests/unit/test_contract.py` checks the contract file itself is
well-formed.

**Adding a tool** therefore means editing the contract, adding the per-tool logic
in each runtime, and adding a test — see [CONTRIBUTING.md](../CONTRIBUTING.md).
You never edit a tool list by hand.

## Capability gating

Some tools only make sense against servers that support them. Rather than
advertising a tool that always fails, each runtime probes the connected OPC UA
server at `tools/list` time and filters:

| Capability | Probe | Gates |
|---|---|---|
| `history` | Read `AccessHistoryDataCapability` (`ns=0;i=11193`) is true | `read_history_opcua_node` |
| `aggregate` | Browse `AggregateFunctions` (`ns=0;i=2997`) is non-empty | `read_aggregate_opcua_node` |

The probes are **best-effort by design**: any failure yields "not supported"
rather than an error. A transient OPC UA outage must not strip the core tools
from `tools/list`.

> There are three mocks, on purpose. The main one (`packages/mock-server/`,
> :4840) enables history and advertises **no** aggregate functions, so the suite
> can assert the aggregate tool stays hidden when unsupported. The second
> (`packages/mock-server-aggregate/`, :4841) advertises aggregates, so the read
> path itself is covered on both runtimes. The third
> (`packages/mock-server-alarms/`, :4842) has a real alarm condition — see below.

The event tools are deliberately **not** gated. Every OPC UA server has a Server
object with an EventNotifier, and a server that raises nothing simply buffers
nothing; there is no capability to probe that would make hiding them more honest
than offering them. A server without Alarms & Conditions is told apart at call
time instead: `list_active_alarms` reports that its ConditionRefresh call failed
and that the server may not implement A&C, rather than returning an empty list a
model would read as "no alarms".

## Events and Alarms & Conditions

MCP is request/response and OPC UA events arrive when the server decides, so the
two are bridged by a buffer rather than by pushing anything at the client:
`subscribe_events` starts a real OPC UA subscription whose monitored item parks
what arrives, and `read_events` drains it. `list_active_alarms` sidesteps the
buffer entirely — it makes its own short-lived subscription, calls
ConditionRefresh, and collects the retained conditions the server replays
between the RefreshStart and RefreshEnd events.

`contract/tools.json` -> `events` is what keeps the two runtimes saying the same
thing: one list of OPC UA browse paths that is simultaneously the EventFilter
select clauses both servers send and the field order of an `eventRecords`
record. Two details of it are load-bearing and non-obvious:

- Every path is resolved against **BaseEventType**, which Part 4 §7.4.4.5 says
  makes a server evaluate it without regard to the event's own type. That is how
  one filter selects `AckedState/Id` from a condition and gets `null` — rather
  than an error — from a plain event, and so how one record shape covers both.
- **ConditionId** is not a component of ConditionType at all; it is the NodeId
  attribute of the condition instance, selected with an empty browse path. It is
  also what `acknowledge_alarm` calls the Acknowledge method on, so getting it
  wrong is not cosmetic — the tool would have nothing to acknowledge.

`acknowledge_alarm` takes only the `event_id` a model has just seen, because both
servers remember which condition each event they reported came from. The
condition can still be passed explicitly for an event that came from somewhere
else.

## The three invariants

**1. `stdout` belongs to the transport.** MCP speaks JSON-RPC over stdio; a stray
`print()` or `console.log` corrupts the stream and the client disconnects with a
parse error. Python logs to `stderr` explicitly; the Node server reassigns
`console.log` to `console.error` at startup, because `node-opcua` logs PKI and
certificate messages on its own.

**2. Nothing hardcodes a version.** The version is single-sourced from each
package manifest — Node stages it into `build/version.json` at build time, Python
reads its installed distribution metadata. `tests/unit/test_version_manifests.py`
fails the build if a literal reappears or the manifests drift apart.

**3. The published artifact is what users get, not the source tree.** Paths that
resolve in a checkout may not resolve in a wheel or a tarball, and a dependency
range that resolves to one major today may resolve to a breaking one tomorrow.
Both have already shipped bugs here. `tests/smoke/` builds the real artifacts,
installs them in isolation, and drives the installed entry points from outside
the repo.

There are now five such artifacts, and the two newest stray furthest from the
source tree: the `.mcpb` bundle inlines the contract and bundles
node-opcua-client's whole CommonJS dependency tree into one file, and the
single-file executables freeze an interpreter around it. Both can break while every other test stays
green, so both are built and driven over MCP in `tests/smoke/`. See
[install.md](install.md) for what each artifact is for.

## Layout

```
contract/tools.json          single source of truth for the tool surface
packages/server-python/      mcp MCPServer + opcua (FreeOpcUa)
  src/opcua_mcp_server/      config · security · contract · datetimes
                             · capabilities · aggregates · records · events
                             · version · install · cli · server
  packaging/                 PyInstaller spec for the single-file executable
packages/server-node/        @modelcontextprotocol/sdk + node-opcua-client
  src/                       config · security · contract · dates · records
                             · events · connection · tools · install · index
                             · sea
  mcpb/manifest.json         MCP bundle manifest (Claude Desktop extension)
  scripts/                   build steps: npm package · .mcpb · executable
packages/mock-server/        simulated PLC/sensors (:4840, no aggregates)
packages/mock-server-aggregate/  aggregate-capable mock (:4841)
packages/mock-server-alarms/     Alarms & Conditions mock (:4842)
tests/                       unit/ (fast) · e2e/ (both servers, secured and not)
                             · smoke/ (artifacts) · fixtures/ (secured mock, PKI)
examples/                    standalone demo scripts
```

## Security posture

Connection security is configured through the environment, by the same variables
on both runtimes: `OPCUA_SECURITY_POLICY`, `OPCUA_SECURITY_MODE`,
`OPCUA_CLIENT_CERT`, `OPCUA_CLIENT_KEY`, `OPCUA_USERNAME` and `OPCUA_PASSWORD`
(see [Configuration](../README.md#configuration)). Each runtime parses and
validates them in one module — `security.ts` / `security.py` — which the client
factory, the capability probes and the startup check all go through, so a
probe cannot end up on a different security footing than the session it
precedes.

The **default is `None`/`None`**: unauthenticated and unencrypted, appropriate
for the bundled mock and local development and **not** appropriate for
production industrial systems. Both servers warn on stderr when running that
way. For what the secured path does and does not verify — notably that the
server certificate is not pinned — see [SECURITY.md](../SECURITY.md).
