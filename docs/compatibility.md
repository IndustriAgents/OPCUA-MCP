# Compatibility

What has actually been exercised, and by what. Everything in the first table is
derived from the test suite in this repository — the fixtures it starts and the
assertions it makes — not from a fresh manual run and not from any vendor
certification. The current result is whatever
[CI](https://github.com/IndustriAgents/OPCUA-MCP/actions/workflows/ci.yml) last
reported on `main`.

Every row below runs against **both** runtimes: each e2e test is parametrised
`["python", "node"]`, so a "covered" cell means covered on Python *and* Node.

## OPC UA servers

| Server | Core reads / writes / browse / methods | History | Aggregates | Data subscriptions | Events | Retained alarms + acknowledge |
|---|---|---|---|---|---|---|
| [Plant mock](../packages/mock-server/README.md) (`:4840/freeopcua/server/`) | Covered | Covered | Not modelled | Covered | Covered | Not modelled |
| [Aggregate mock](../packages/mock-server-aggregate/README.md) (`:4841/UA/Aggregate`) | Not exercised | Not exercised | Covered | Not exercised | Not exercised | Not modelled |
| [Alarms mock](../packages/mock-server-alarms/README.md) (`:4842/UA/Alarms`) | Writes only, to trigger alarms | Not modelled | Not modelled | Not exercised | Covered | Covered |
| [Secured fixture](../tests/fixtures/secure_opcua_server.py) | Reads and writes over `Basic256Sha256` | Not exercised | Not modelled | Not exercised | Not exercised | Not modelled |
| Any third-party server | Unverified | Unverified | Unverified | Unverified | Unverified | Unverified |

- **Covered** — an automated test asserts the behaviour on both runtimes.
- **Not modelled** — the fixture does not implement that OPC UA capability, so
  nothing could be tested against it. Two of these are themselves assertions:
  the plant mock is what proves the aggregate tool is *hidden* when unsupported,
  and that `list_active_alarms` says so plainly against a server with no
  condition model.
- **Not exercised** — the capability may exist, but no test uses it there.
- **Unverified** — no result has been recorded here. See
  [Report a result](#report-a-result).

No third-party OPC UA server — Prosys, Ignition, KEPServerEX, Siemens, Beckhoff
or any other — has a recorded result. Both runtimes use general-purpose OPC UA
client libraries (`python-opcua` and `node-opcua-client`) and speak no
vendor-specific protocol, so they are *expected* to work against a compliant
server; expected is not tested. Server version, licensed features and the
permissions of the account you connect with all change the answer.

## Runtimes and clients

- **Python 3.10+**, **Node 22.13+** — the floors the manifests declare.
- CI runs Python 3.10 with Node 22, Python 3.13 with Node 22, and Python 3.13
  with Node 24.
- **Transport: stdio only.** The client must be able to launch a local stdio MCP
  server, or install the Claude Desktop `.mcpb` bundle. There is no HTTP
  transport ([#14](https://github.com/IndustriAgents/OPCUA-MCP/issues/14)).
- **What reaches the plant is identical on both runtimes.** Date/time parsing,
  write conversion per data type, method-argument typing, OPC UA status severity
  and the operator policy's number parsing are each one table under
  [`tests/fixtures/`](../tests/fixtures/) that both runtimes' unit suites run
  ([#157](https://github.com/IndustriAgents/OPCUA-MCP/issues/157)). Timestamps
  must carry a zone (`Z` or an offset); one without is refused on both.
- Client configurations in [docs/install.md](install.md) are worked
  examples, not a per-client certification matrix. `--install claude-desktop`
  is the only client integration with its own tests.

## Runtime differences

Both runtimes are first-class
([ADR 0001](adr/0001-two-first-class-runtimes.md)): one contract, one suite,
one release gate, one version. What they do **not** share is declared in
[`contract/runtime-differences.json`](../contract/runtime-differences.json),
which is the authoritative list; this table renders it, and
`tests/unit/test_runtime_differences.py` fails if an id is missing here or a
checkable fact behind an entry changes. Every entry is deliberate — allowed by
the ADR and expected to stay until a library change retires it (usually
[#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144), the move to
`asyncua`).

| ID | What differs | Could retire it |
|---|---|---|
| `security-policies-aes` | `Aes128_Sha256_RsaOaep` and `Aes256_Sha256_RsaPss` are Node only; Python refuses them at startup, naming the Node runtime | [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) |
| `certificate-file-encoding` | Python reads a certificate or key as PEM only when it is named `*.pem`; Node sniffs the contents. Name every file `*.pem` and both work | [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) |
| `mcp-protocol-generation` | Python's `mcp` 2.x serves MCP protocol revisions up to 2026-07-28; Node's SDK 1.x up to 2025-11-25. Same tools and results either way; change notifications are offered on neither | — |
| `mcpb-bundle` | The Claude Desktop `.mcpb` bundle is Node only | — |
| `mcp-registry-listing` | The MCP Registry metadata (`server.json`) lists the npm package only | — |
| `cli-command-alias` | npm also installs the command `opcua-mcp`; the documented `opcua-mcp-server` is on both | — |
| `reconnect-mechanism` | node-opcua repairs a dropped channel in the background and keeps the same session; Python builds a new session when the next call needs one. Same settings, same waits, same recovery | [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) |
| `transport-limit-enforcement` | The same inbound message bounds, enforced by node-opcua on Node and by a local patch to python-opcua on Python (CVE-2022-25304) | [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) |
| `opcua-library-maintenance` | Python's OPC UA library is unmaintained; Node's is maintained. See [SECURITY.md](../SECURITY.md#cve-2022-25304--unbounded-chunk-reassembly-in-python-opcua) | [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) |
| `client-pki-on-disk` | node-opcua keeps a PKI folder of its own, with a generated default certificate and the server certificates it has seen; Python writes nothing | — |
| `user-key-handling` | The X.509 user key signs through a key provider on Node and is loaded into memory on Python | [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) |
| `library-reason-text` | The `{reason}` inside an error, and `get_server_status`'s `error`, are in each client library's own words; the rest of every message is identical | — |
| `concurrency-model` | Latency, throughput and memory differ and are not promised to match; the bounds and waits are | [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) |

### Known divergences being fixed

Anything a client can observe that is not in the table above is a bug, not a
difference — report it with the bug template's *both runtimes, behaving
differently* box. Those known today are **not** allowed differences and are
being fixed in both runtimes:

- [#157](https://github.com/IndustriAgents/OPCUA-MCP/issues/157) — found while
  writing ADR 0001. The group that reached the plant is fixed: dates, write
  conversion, method-argument types, Good-subcode statuses and the policy's
  number parsing now follow one rule on both runtimes (see
  [Runtimes and clients](#runtimes-and-clients)). Still open: result and error
  formatting, subscription parameters, policy-file parsing, signal handling and
  `--install` details.
- [#136](https://github.com/IndustriAgents/OPCUA-MCP/issues/136) — with
  `OPCUA_RECONNECT_MAX_RETRY=-1` and an unreachable endpoint, the Node server may
  never open its MCP transport, and the numeric reconnect settings accept
  different spellings on each runtime.

Until #136 is fixed, do not rely on `OPCUA_RECONNECT_MAX_RETRY=-1` with the
Node runtime.

## Security coverage

[`tests/e2e/test_secure_connection_e2e.py`](../tests/e2e/test_secure_connection_e2e.py)
drives both runtimes against a fixture that offers `Basic256Sha256` endpoints
only, with certificate-based channel security, username and X.509 user
authentication, and checks that an unsecured client is refused, that passwords
never reach the logs, and that a pinned server certificate (`OPCUA_SERVER_CERT`)
connects while an impostor's is refused.
[`test_security_startup.py`](../tests/e2e/test_security_startup.py) checks that
an unusable security configuration is rejected at startup.

That is coverage of *this project's* handling of security, not of interoperation
with a vendor PKI. Without `OPCUA_SERVER_CERT` the server certificate is taken
from the endpoint description and not verified, and neither runtime validates
against a CA trust list — see [SECURITY.md](../SECURITY.md#connection-security-important)
and [#134](https://github.com/IndustriAgents/OPCUA-MCP/issues/134).

## Tool policy coverage

[`tests/e2e/test_policy_e2e.py`](../tests/e2e/test_policy_e2e.py) runs both
runtimes under the default `observe` profile and under `operator` with explicit
allowlists, and checks that a hidden control tool is still refused when called
directly. Policy is enforced per call, not only at tool-listing time.

## Resilience and diagnostics coverage

[`tests/e2e/test_resilience_e2e.py`](../tests/e2e/test_resilience_e2e.py) drops a
mock server under both runtimes and asserts the session recovers, including the
data-change subscriptions held across the outage, and that a server unreachable
at startup does not stop either server from starting.
[`test_diagnostics_e2e.py`](../tests/e2e/test_diagnostics_e2e.py) checks that
`get_server_status` answers while disconnected and that both runtimes return the
same fields. The backoff arithmetic itself is pinned in
[`tests/unit/test_reconnect.py`](../tests/unit/test_reconnect.py).

This is coverage against the local mocks. How a given third-party server behaves
across a real network outage — session lifetimes, certificate rotation on
restart, subscription re-establishment — is **Unverified**.

## Evidence

| Area | Tests |
|---|---|
| Core operations, history, subscriptions | [`test_mcp_e2e.py`](../tests/e2e/test_mcp_e2e.py) |
| Contract and result-shape parity | [`test_contract_parity.py`](../tests/e2e/test_contract_parity.py) |
| Aggregates | [`test_aggregate_e2e.py`](../tests/e2e/test_aggregate_e2e.py) |
| Events and Alarms & Conditions | [`test_events_e2e.py`](../tests/e2e/test_events_e2e.py) |
| Tool policy | [`test_policy_e2e.py`](../tests/e2e/test_policy_e2e.py) |
| Reconnection and resilience | [`test_resilience_e2e.py`](../tests/e2e/test_resilience_e2e.py), [`test_reconnect.py`](../tests/unit/test_reconnect.py) |
| Health and diagnostics | [`test_diagnostics_e2e.py`](../tests/e2e/test_diagnostics_e2e.py) |
| Channel security | [`test_secure_connection_e2e.py`](../tests/e2e/test_secure_connection_e2e.py), [`test_security_startup.py`](../tests/e2e/test_security_startup.py) |
| Published artifacts | [`tests/smoke/`](../tests/smoke) |
| Fixtures and ports | [`tests/conftest.py`](../tests/conftest.py) |

## Report a result

Open a
[compatibility report](https://github.com/IndustriAgents/OPCUA-MCP/issues/new?template=compatibility_report.md).
A row moves out of **Unverified** only with a linked report that names the server
product and version, the package version, the runtime, the MCP client, the
security and identity settings, and the tools that were called.

Test only on equipment you are authorised to use; use a simulator or an isolated
lab for writes, method calls and alarm acknowledgements. Do not include
credentials, private keys, internal endpoint names or production process data in
a report.
