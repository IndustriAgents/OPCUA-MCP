# Roadmap

What exists, what is being worked on next, and what is only an idea. This is an
order of work, not a schedule: nothing here carries a release date.

The manifests are at **0.3.0**. [CHANGELOG.md](CHANGELOG.md) records what has
actually shipped — including entries under `[Unreleased]`, which are merged but
not yet published to npm or PyPI. The
[v0.4.0 engineering plan](docs/ROADMAP-0.4.0.md) is the phased plan currently
being worked; the [v0.2.0 plan](docs/ROADMAP-0.2.0.md) is finished work, kept
for context; and [#19](https://github.com/midhunxavier/OPCUA-MCP/issues/19) is
the feature epic this page summarises.

## In the codebase today

- Python and Node implementations of one shared tool contract
  ([`contract/tools.json`](contract/tools.json)), kept in step by parity tests.
- Reads, batch reads, writes, browsing, method calls and address-space
  inventory, with typed writes and bounded discovery.
- History and server-side aggregates, offered only when the connected server
  advertises the capability.
- Data-change subscriptions with buffered records, plus event collection and
  Alarms & Conditions listing and acknowledgement.
- Configurable OPC UA channel security: policy, mode, client certificate and
  key, and username identity, validated at startup.
- An observe-only default tool profile, with `operator` node and method
  allowlists, a versioned JSON policy file, and control tools blocked on an
  unsecured channel unless a lab override is explicit.
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

Phases 1–8 of the [v0.4.0 engineering plan](docs/ROADMAP-0.4.0.md), which closes
the correctness and consistency findings from the 0.3.0 architecture review and
consolidates the tool surface from 17 tools to 13. In merge order:

| # | Work | Done when |
|---|---|---|
| 1 | [MCP Registry listing](docs/mcp-registry.md) | A published package carries `mcpName`, and the entry resolves in the registry |
| 2 | Results from third-party OPC UA servers ([#70](https://github.com/midhunxavier/OPCUA-MCP/issues/70)) | [docs/compatibility.md](docs/compatibility.md) records dated, versioned results for at least two non-mock servers |
| 3 | Two correctness fixes (plan phases 1–2) | A failed batch read is an error on both runtimes, and Node drains browse continuation points instead of silently returning a short list |
| 4 | Server-certificate verification ([#45](https://github.com/midhunxavier/OPCUA-MCP/issues/45)) and X.509 user authentication ([#7](https://github.com/midhunxavier/OPCUA-MCP/issues/7)) (phase 3) | Pinning or trust-list validation closes the gap named in [SECURITY.md](SECURITY.md), and a *wrong* server certificate is refused by a test |
| 5 | Contract-derived policy guards and node-ID canonicalisation (phase 4) | A control tool with no declared guard is denied, and allowlists key off namespace URIs rather than session-assigned indexes |
| 6 | Tool consolidation and a declared result shape for every tool (phase 5) | 13 tools, all with a `resultShape`, and a test that diffs the two runtimes' output against each other rather than only against the contract. Closes [#8](https://github.com/midhunxavier/OPCUA-MCP/issues/8) and [#9](https://github.com/midhunxavier/OPCUA-MCP/issues/9) |

Items 1 and 2 need no code, only a release and reports from people with real
equipment. Everything else is code and tests.

## Later, not started

Tracked as issues, in no committed order: node search and browse-path
resolution ([#11](https://github.com/midhunxavier/OPCUA-MCP/issues/11)), typed
method arguments from `InputArguments` metadata
([#10](https://github.com/midhunxavier/OPCUA-MCP/issues/10)), full node
attribute reads ([#8](https://github.com/midhunxavier/OPCUA-MCP/issues/8)), and
X.509 user authentication
([#7](https://github.com/midhunxavier/OPCUA-MCP/issues/7)).

## Considered and set aside

Closed rather than left open indefinitely, because an open issue with no
intended start date reads as a commitment. Each would be reopened if its
condition below is met.

| Work | Set aside because | Reopen when |
|---|---|---|
| Streamable-HTTP transport ([#14](https://github.com/midhunxavier/OPCUA-MCP/issues/14)) | The tool policy is enforced per process and has no notion of *who* is calling; an HTTP listener would make it a remote endpoint that can write to a PLC | Per-client authorisation has a design |
| Multiple or file-configured endpoints ([#15](https://github.com/midhunxavier/OPCUA-MCP/issues/15)) | A per-tool endpoint argument would make `writable_nodes` mean different physical nodes per server, on keys that are already session-scoped. One process per endpoint costs nothing today and keeps a misconfigured policy to one PLC | Node-ID canonicalisation and URI-based allowlists have landed |
| Docker images ([#16](https://github.com/midhunxavier/OPCUA-MCP/issues/16)) | Four distribution channels already ship, and a container adds little for a stdio server that runs beside its client | A remote transport lands, or a deployment requires an image |

One thing deliberately has no plan yet: an audit trail that records control
decisions without leaking credentials or process values. It needs a design that
holds for the Python and Node runtimes and the shared contract before any code
is written. Per-client approval semantics for control tools are now the stated
prerequisite for [#14](https://github.com/midhunxavier/OPCUA-MCP/issues/14) and
are tracked there.

## Helping

The most useful contribution is a result from a real server:

- Open a
  [compatibility report](https://github.com/midhunxavier/OPCUA-MCP/issues/new?template=compatibility_report.md)
  for an OPC UA server that is not one of the mocks.
- Report where the mock walkthrough in [docs/testing.md](docs/testing.md) leaves
  you stuck — onboarding bugs are bugs.
- Pick up an issue above; [CONTRIBUTING.md](CONTRIBUTING.md) covers layout,
  the tool contract, and how to add a tool to both servers at once.

Test only on equipment you are authorised to use, and keep writes, method calls
and alarm acknowledgements to a simulator or an isolated lab.
