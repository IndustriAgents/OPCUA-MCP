# Compatibility and test coverage

Coverage below is derived from the repository's fixtures and test source. It is
not a fresh test run or a vendor certification. Both MCP runtimes are exercised
by the relevant end-to-end tests; the latest result is available in [CI](https://github.com/midhunxavier/OPCUA-MCP/actions/workflows/ci.yml).

**Covered** means an automated test exists for that operation. **Not provided**
means the fixture does not implement that capability. **Not established** means
no result is recorded here.

| OPC UA server / fixture | Core reads, writes, browse, methods | Raw history | Aggregates | Data subscriptions | Plain events | Retained alarms / acknowledgement |
|---|---|---|---|---|---|---|
| [Main industrial mock](../packages/mock-server/README.md) | Covered | Covered | Not provided | Covered | Covered | Not provided |
| [Aggregate mock](../packages/mock-server-aggregate/README.md) | Not established as a group | Not established | Covered | Not established | Not established | Not established |
| [Alarms mock](../packages/mock-server-alarms/README.md) | Temperature reads/writes used by alarm tests | Not provided | Not established | Not established | Not established | Covered |
| Prosys OPC UA Simulation Server | Unverified | Unverified | Unverified | Unverified | Unverified | Unverified |
| Ignition | Unverified | Unverified | Unverified | Unverified | Unverified | Unverified |
| Kepware | Unverified | Unverified | Unverified | Unverified | Unverified | Unverified |
| Siemens S7 OPC UA server | Unverified | Unverified | Unverified | Unverified | Unverified | Unverified |
| Beckhoff TwinCAT OPC UA server | Unverified | Unverified | Unverified | Unverified | Unverified | Unverified |

The vendor rows are candidates for community testing, not claims of support or
endorsement. Server version, enabled features, licenses, and account permissions
can change the result.

## Important distinctions

- The main mock uses `opc.tcp://localhost:4840/freeopcua/server/`.
  It raises plain events but has no OPC UA condition model.
- The aggregate mock uses `opc.tcp://localhost:4841/UA/Aggregate`.
- The alarms mock uses `opc.tcp://localhost:4842/UA/Alarms`.
  It supports retained conditions and acknowledgement.
- History and aggregate tools are offered only when the connected server
  advertises the relevant capability.
- Event subscriptions buffer events; clients retrieve them with `read_events`.
  The MCP server does not independently send a chat notification.

## Security coverage

[Secure-connection tests](../tests/e2e/test_secure_connection_e2e.py) use a separate
fixture with encrypted endpoints and username authentication. This does not
establish compatibility with a vendor PKI or full trust-list verification.
Read [SECURITY.md](../SECURITY.md), including the server-certificate verification gap.

## Runtime and client scope

- Python: 3.10+; Node: 22.13+.
- CI includes Python 3.10/3.13 with Node 22, plus Python 3.13 with Node 24.
- Transport: local stdio. A client must support stdio MCP servers or the documented
  Claude Desktop bundle installation.
- Client setup examples are not a versioned client certification matrix.

## Evidence

- [Core operations and subscriptions](../tests/e2e/test_mcp_e2e.py)
- [History aggregates](../tests/e2e/test_aggregate_e2e.py)
- [Events and Alarms & Conditions](../tests/e2e/test_events_e2e.py)
- [Fixture definitions](../tests/conftest.py)
- [CI configuration](../.github/workflows/ci.yml)

## Add a result

Open a [compatibility report](https://github.com/midhunxavier/OPCUA-MCP/issues/new?template=compatibility_report.md).
Include server/version, MCP package version, runtime, client/version, tested
operations, authentication/security mode, and a reproducible sanitized transcript.
Only mark a capability verified after linking the evidence and date. Do not post
credentials, private keys, internal endpoint names, or production process data.
