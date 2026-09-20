# Roadmap

What exists, what is being worked on next, and what is only an idea. This is an
order of work, not a schedule: nothing here carries a release date.

The manifests are at **0.4.1**. [CHANGELOG.md](CHANGELOG.md) records what has
actually shipped — including entries under `[Unreleased]`, which are merged but
not yet published to npm or PyPI. The
[v0.4.0 engineering plan](docs/ROADMAP-0.4.0.md) is the phased plan currently
being worked; the [v0.2.0 plan](docs/ROADMAP-0.2.0.md) is finished work, kept
for context; and [#19](https://github.com/IndustriAgents/OPCUA-MCP/issues/19) is
the feature epic this page summarises.

## In the codebase today

- Python and Node implementations of one shared tool contract
  ([`contract/tools.json`](contract/tools.json)), kept in step by parity tests —
  every tool declares a result shape, each runtime's actual output is checked
  against it, and a differential suite then diffs the two runtimes against each
  other.
- Thirteen tools. Reads, writes, browsing, path resolution, name search, method
  calls and address-space inventory, each with fully qualified records: a value
  arrives with its data type, OPC UA status and timestamps.
- History and server-side aggregates in one tool, offered when the connected
  server advertises either capability.
- Data-change subscriptions with buffered records, plus event collection and
  Alarms & Conditions listing and acknowledgement.
- Configurable OPC UA channel security: policy, mode, client certificate and
  key, username or X.509 user identity, and a pinned server certificate, all
  validated at startup.
- An observe-only default tool profile, with `operator` node and method
  allowlists, a versioned JSON policy file, and control tools blocked on an
  unsecured channel unless a lab override is explicit. Authorisation is derived
  from a `guard` each control tool declares in the contract, so a tool that
  declares none is denied rather than waved through, and allowlists can be
  pinned by namespace URI rather than by an index the server may renumber.
- Automatic reconnection with keep-alive and exponential backoff, re-creating
  data-change subscriptions on the new session, so neither server needs
  restarting when the OPC UA server does.
- A `get_server_status` tool reporting connection state, endpoint and security,
  the server's own `ServerStatus` and its namespace array — the one tool that
  answers while disconnected.
- Distribution as an npm package, a PyPI package, a Claude Desktop `.mcpb`
  bundle and single-file executables, each covered by artifact smoke tests.
- Three local mock OPC UA servers and an end-to-end suite that runs both
  runtimes against them in CI.

## Next

The [v0.4.0 engineering plan](docs/ROADMAP-0.4.0.md) is **complete** — all eight
phases shipped, closing #7, #8, #9, #10, #11, #45, #75 and #76. What remains
needs no code:

| # | Work | Done when |
|---|---|---|
| 1 | [MCP Registry listing](docs/mcp-registry.md) | A published package carries `mcpName`, and the entry resolves in the registry |
| 2 | Results from third-party OPC UA servers ([#70](https://github.com/IndustriAgents/OPCUA-MCP/issues/70)) | [docs/compatibility.md](docs/compatibility.md) records dated, versioned results for at least two non-mock servers |

Both need a release and reports from people with real equipment rather than
changes to this repository.

## Later, not started

Nothing is queued. Everything that was here at 0.3.0 — node search and
browse-path resolution, typed method arguments, full node attribute reads, X.509
user authentication — shipped in 0.4.0, and the architecture review that opened
#81–#90 is closed out too: the contract is now the advertised surface on both
runtimes and the thing enforcing arguments, failures are worded from it, the
catalogue no longer waits on the plant, the requests that had no bounds have them,
chunk reassembly is bounded against CVE-2022-25304, and CI runs on Windows and
macOS.

The next thing worth doing is decided by what
[#70](https://github.com/IndustriAgents/OPCUA-MCP/issues/70) turns up: a result
from a real vendor server is the one input this repository cannot generate for
itself, and it is more likely to set priorities usefully than anything that could
be written down now. It is also what decides the one question the review left
open rather than answered — whether this should serve a plant or a workstation
(see below).

## Considered and set aside

Closed rather than left open indefinitely, because an open issue with no
intended start date reads as a commitment. Each would be reopened if its
condition below is met.

| Work | Set aside because | Reopen when |
|---|---|---|
| Streamable-HTTP transport ([#14](https://github.com/IndustriAgents/OPCUA-MCP/issues/14)) | The tool policy is enforced per process and has no notion of *who* is calling; an HTTP listener would make it a remote endpoint that can write to a PLC | Per-client authorisation has a design |
| Multiple or file-configured endpoints ([#15](https://github.com/IndustriAgents/OPCUA-MCP/issues/15)) | **Its condition has been met — see below.** | Superseded |
| Docker images ([#16](https://github.com/IndustriAgents/OPCUA-MCP/issues/16)) | Four distribution channels already ship, and a container adds little for a stdio server that runs beside its client | A remote transport lands, or a deployment requires an image |

### Multiple endpoints: the condition was met, and the answer is still not yet

#15 was set aside on a stated condition — "Node-ID canonicalisation and URI-based
allowlists have landed" — because the objection was that a per-tool endpoint
argument would make `writable_nodes` mean different physical nodes per server, on
keys that are session-scoped. Both landed in 0.4.0 (`node_ids.py` and its Node
twin canonicalise spelling; `nsu=<uri>;i=…` entries resolve against the live
NamespaceArray on every connect). So the condition is met and the objection is
gone, which means the honest thing is to say what the *remaining* reason is
rather than leave a lapsed condition standing.

The remaining reason is that a second endpoint is not one argument, it is a
different product. Today `OPCUA_SERVER_URL` is process-global, every tool targets
it implicitly, stdio is the only transport, and each MCP client gets its own OPC
UA session — which on equipment where sessions are licensed is a cost per client
per endpoint. Serving a plant rather than a workstation needs, together:

- an endpoint registry, and an endpoint argument on every tool that names a node;
- policy scoped per endpoint, since `writable_nodes` for PLC A must not authorise
  anything on PLC B — which is the same missing notion of *who is asking* that
  set aside [#14](https://github.com/IndustriAgents/OPCUA-MCP/issues/14);
- pooled sessions, so N clients do not mean N sessions per PLC;
- a transport that can serve more than one client.

That is one piece of work, not four, and #14 is half of it. The tool signatures
are the part that would have to change, and they were only just stabilised at
0.4.0 — so the sequencing that makes sense is to let
[#70](https://github.com/IndustriAgents/OPCUA-MCP/issues/70) come back from real
equipment first: whether sessions are actually scarce, and whether anyone is
trying to run this for a line rather than for themselves, decides whether this is
worth the tool-surface break. Until then, one process per endpoint is not a
workaround, it is the design, and [README.md](README.md#overview) now says so
where someone would meet it.

**Reopen when** a compatibility report or a user says they are running this
against more than one endpoint at once, or per-client authorisation gets a design
(which is #14's condition and this one's too).

### The control audit trail, as it actually stands

This page used to say an audit trail had no plan yet. It ships, and has since
the policy layer landed — so here is what it does and does not do, which is more
useful than either claim.

Every `control` and `alarm-action` call writes one JSON line to **stderr**:

```json
{"event":"opcua_mcp_policy","timestamp":"2026-09-18T09:12:44.001Z","call_id":"9f2c1ab4de77f031",
 "profile":"operator","tool":"write_opcua_nodes","decision":"allowed","node_ids":["ns=2;i=13"]}
```

`decision` is one of `allowed`, `denied`, `completed` or `failed` — the outcome
as well as the verdict, because "permitted" and "happened" are different facts
and the gap between them is where a control call that reached the plant and then
failed lives. `call_id` is what joins a call's lines to each other, which they had
no way to be until [#87](https://github.com/IndustriAgents/OPCUA-MCP/issues/87):
both runtimes serve calls concurrently, so overlapping writes interleave, and two
writes to the same node were not distinguishable by content. The targets come from the same `guard` declaration in
`contract/tools.json` that the policy authorises from, so the two cannot disagree
about which arguments matter. Reads are never audited; a trail that recorded
every read would bury the lines anyone is looking for. Neither credentials nor
written values appear, and a test asserts it.

**What it is not** is durable. stderr is what an MCP client shows the user and
what a log collector picks up, and nothing here writes a file, rotates one, or
survives the process. For a deployment that needs a retained record, collect the
server's stderr — the format is stable and line-oriented for exactly that. A
built-in persistent sink still has no plan, and needs one that holds for both
runtimes before any code is written.

Per-client approval semantics for control tools are the stated prerequisite for
[#14](https://github.com/IndustriAgents/OPCUA-MCP/issues/14) and are tracked there.

## Helping

The most useful contribution is a result from a real server:

- Open a
  [compatibility report](https://github.com/IndustriAgents/OPCUA-MCP/issues/new?template=compatibility_report.md)
  for an OPC UA server that is not one of the mocks.
- Report where the mock walkthrough in [docs/testing.md](docs/testing.md) leaves
  you stuck — onboarding bugs are bugs.
- Pick up an issue above; [CONTRIBUTING.md](CONTRIBUTING.md) covers layout,
  the tool contract, and how to add a tool to both servers at once.

Test only on equipment you are authorised to use, and keep writes, method calls
and alarm acknowledgements to a simulator or an isolated lab.
