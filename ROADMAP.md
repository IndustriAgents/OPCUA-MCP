# Roadmap

What exists, what is being worked on next, and what is only an idea. This is an
order of work, not a schedule: nothing here carries a release date.

The manifests are at **0.3.0**. [CHANGELOG.md](CHANGELOG.md) records what has
actually shipped — including entries under `[Unreleased]`, which are merged but
not yet published to npm or PyPI. The
[v0.2.0 engineering plan](docs/ROADMAP-0.2.0.md) is finished work, kept for
context, and [#19](https://github.com/midhunxavier/OPCUA-MCP/issues/19) is the
feature epic this page summarises.

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
- Distribution as an npm package, a PyPI package, a Claude Desktop `.mcpb`
  bundle and single-file executables, each covered by artifact smoke tests.
- Three local mock OPC UA servers and an end-to-end suite that runs both
  runtimes against them in CI.

## Next

| # | Work | Done when |
|---|---|---|
| 1 | [MCP Registry listing](docs/mcp-registry.md) | A published package carries `mcpName`, and the entry resolves in the registry |
| 2 | Results from third-party OPC UA servers ([#70](https://github.com/midhunxavier/OPCUA-MCP/issues/70)) | [docs/compatibility.md](docs/compatibility.md) records dated, versioned results for at least two non-mock servers |
| 3 | Connection resilience — reconnect, keep-alive, backoff ([#18](https://github.com/midhunxavier/OPCUA-MCP/issues/18)) | A dropped session recovers without restarting the MCP process, proven by a test |
| 4 | Health and diagnostics tool ([#13](https://github.com/midhunxavier/OPCUA-MCP/issues/13)) | One tool reports connection state, endpoint, namespaces and active policy |
| 5 | Server-certificate verification ([#45](https://github.com/midhunxavier/OPCUA-MCP/issues/45)) | Trust-list validation or explicit pinning, closing the gap named in [SECURITY.md](SECURITY.md) |

Items 3 and 4 have work in progress; 1 and 2 need no code, only a release and
reports from people with real equipment.

## Later, not started

Tracked as issues, in no committed order: node search and browse-path
resolution ([#11](https://github.com/midhunxavier/OPCUA-MCP/issues/11)), typed
method arguments from `InputArguments` metadata
([#10](https://github.com/midhunxavier/OPCUA-MCP/issues/10)), full node
attribute reads ([#8](https://github.com/midhunxavier/OPCUA-MCP/issues/8)),
X.509 user authentication
([#7](https://github.com/midhunxavier/OPCUA-MCP/issues/7)), multiple or
file-configured endpoints
([#15](https://github.com/midhunxavier/OPCUA-MCP/issues/15)), a
Streamable-HTTP transport
([#14](https://github.com/midhunxavier/OPCUA-MCP/issues/14)), and Docker images
([#16](https://github.com/midhunxavier/OPCUA-MCP/issues/16)).

Two things deliberately have no plan yet: an audit trail that records control
decisions without leaking credentials or process values, and per-client approval
semantics for control tools. Both need a design that holds for the Python and
Node runtimes and the shared contract before any code is written.

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
