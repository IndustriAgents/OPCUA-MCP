# OPC UA MCP roadmap

This is a proposed order of work, not a release-date commitment. Current package
manifests are at 0.3.0. The [v0.2.0 engineering plan](docs/ROADMAP-0.2.0.md)
is historical context; the [changelog](CHANGELOG.md) records shipped changes.

## Available in the current codebase

- Python and Node implementations of the shared MCP tool contract.
- Node reads/writes, browsing, method calls, subscriptions, and event collection.
- History and aggregates when the connected server advertises support.
- Retained alarms and acknowledgements on Alarms & Conditions servers.
- npm/PyPI packaging, Claude Desktop bundle, standalone executable builds.
- Local mocks and automated coverage for both runtimes.
- Configurable connection security, with limitations documented in [SECURITY.md](SECURITY.md).

## Next: help engineers evaluate the project

| Priority | Deliverable | Completion evidence |
|---|---|---|
| 1 | A reproducible first-use demo | A real recording of the mock setup, a sensor read, and subscription results; a short GIF extracted from it |
| 2 | Official MCP Registry listing | A newly published package carrying matching registry verification metadata, and a discoverable registry entry |
| 3 | External compatibility reports | Versioned results from at least two third-party OPC UA servers, including account permissions and unsupported operations |
| 4 | Clear onboarding | A new user completes the mock guide without maintainer assistance |

The [demo production kit](docs/marketing/demo.md) provides the recording script.
The [registry guide](docs/mcp-registry.md) describes the prepared metadata and the
remaining publication steps. The [compatibility matrix](docs/compatibility.md)
distinguishes existing test coverage from unverified integrations.

## Proposed follow-up: control and trust

These are proposals, not available safeguards:

- Server-certificate trust-list validation or explicit certificate pinning.
- A read-only tool profile and explicit opt-in to writes and method calls.
- Node allowlists and operation limits.
- Audit records that avoid credentials and sensitive process values.
- Documented approval behavior for control operations in supported clients.

Scope these changes against both runtimes and the shared contract before
implementation. Current permissions remain those of the connected OPC UA account.

## How to help

Submit a [compatibility report](https://github.com/midhunxavier/OPCUA-MCP/issues/new?template=compatibility_report.md),
report an onboarding failure, or propose a focused change through the
[contribution guide](CONTRIBUTING.md). Test only on equipment you are authorized
to access; use an isolated simulator for write and method-call experiments.
