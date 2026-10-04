# Maintained Python adapter migration

The Python runtime uses the maintained asyncua client by default behind the nine
application ports extracted in #141. `OPCUA_PYTHON_BACKEND=legacy` selects the
previous client as a rollback path for one release if an uncovered vendor
incompatibility appears. Invalid selections refuse startup. Tool names, request
schemas, policy/audit requirements and retry rules remain shared with Node.

The tested asyncua range is `>=2.0.1,<2.1`; widening this minor ceiling requires
requalifying receive and lifecycle hooks. The maintained path does not patch
library classes. It owns a bounded protocol per client and disables autonomous
watchdogs before they run, while keeping native secure-channel renewal. The
application owns recovery, fresh authorization and subscription reattachment.

## Compatibility and qualification matrix

The maintained backend's complete required suite exercises the same scenarios
as the rollback and Node runtimes. These are repository/mock qualifications;
existing dated vendor reports retain their original SDK identities and do not
establish new vendor compatibility for this adapter.

| Required feature | Evidence |
|---|---|
| Browse and continuation release | Shared browse ports and MCP traversal/continuation tests |
| Batch read/write and Variant typing | All shared native value cases, nonempty request encodings, whole-batch policy and per-item outcomes |
| Methods and InputArguments | Native Argument encoding, derived Duration live method and existing MCP method tests |
| Raw and aggregate history | Live raw history, aggregate native child discovery and complete aggregate paging/release tests |
| Event history | Existing paged event archive tests and corrected standard capability node i=11242 |
| Data-change subscriptions and deadband | Native monitored items, absolute/percent filters, EURange preflight and existing buffer/cancellation tests |
| Alarms & Conditions | Full refresh, acknowledge/confirm/comment and shelving/enable action suite against the alarms mock |
| Security policies, exact pin, username and X.509 users | Complete secured handshake, identity mismatch and authentication refusal suite |
| Namespace URI and reconnect generation | Existing restart, namespace/policy rebinding, capability invalidation and session-generation tests |
| Message/chunk bounds and negotiation | Native hostile split headers, count/byte reassembly caps, exact boundaries, memory release and zero/enlarged Ack tests |
| Keepalive and renewal | Native short-lifetime secure-channel renewal plus application-owned reconnect/subscription restoration tests |
| Timestamps, statuses and structures | Shared native value fixtures and differential suite; timestamp precision and SDK reason text remain declared differences |
| Python 3.10 and 3.13 | Locked full-suite CI matrix plus native transport/service/security floor checks |
| Dual backend CI and rollback | Default conformance uses asyncua; separately named Python 3.10/3.13 legacy jobs retain distinct evidence artifacts |
| Wheel and executable | Installed wheel and frozen executable smoke scenarios select both clients and perform actual reads |

## Receive boundary

The protocol retains at most one bounded chunk while collecting split network callbacks. It reads the eight-byte header first, then refuses an oversized chunk or an over-budget secure message before retaining its body. It feeds complete chunks to asyncua's native parser; native sequence, request, channel, security token, signature and encryption checks remain active. A refusal clears partial and assembled buffers, fails pending requests and closes the channel. Application recovery continues to own whether a later logical request may reconnect or resend.

This boundary is necessary because [asyncua 2.0.1's transport](https://github.com/FreeOpcUa/opcua-asyncio/blob/v2.0.1/asyncua/common/connection.py) overwrites local limits from the peer Ack and checks chunk count without a cumulative wire-byte cap; its [socket protocol](https://github.com/FreeOpcUa/opcua-asyncio/blob/v2.0.1/asyncua/client/ua_client.py) waits for the advertised body before checking the chunk size. The earlier [compatibility spike PR #174](https://github.com/IndustriAgents/OPCUA-MCP/pull/174) is exploratory evidence; the completed application-port and differential suites supersede its old feature failures.

Run `uv run pytest -q tests/unit/test_asyncua_transport.py` to exercise native frames, including byte-at-a-time Acks, headers with no body, multiple chunks in one callback, repeated complete messages and Hello timeout cleanup. These tests do not establish full client or vendor compatibility.

## Native value boundary

`adapters/asyncua_values.py` converts fields of locally constructed request DTOs into maintained native types. The maintained library owns binary encoding and network response parsing; the legacy DTOs remain an internal compatibility boundary for rollback. Nonempty request checks retain node IDs, typed method/write values, aggregate parameters and opaque continuation points. Standard native structures use library-known class identities and bounded field traversal without evaluating annotations. Unknown/custom structures remain explicitly undecodable. `adapters/asyncua_services.py` owns a private maintained SDK loop behind the existing async application ports. Live core checks cover engineering metadata, typed writes, derived method arguments, raw history and filtered subscriptions; native failures retain numeric status/cause classification and failed connection attempts stop their owned loop. MCP backend selection is explicit and covered by factory/patch-isolation tests. Separate maintained-backend CI artifacts record the selected Python client, and release aggregation refuses mixed-backend matrices. The full required suite covers security, events/alarms and recovery; packaged smoke exercises both default and rollback clients. Legacy DTOs and the rollback wire client remain dependencies for this release, and only the rollback factory installs their transport guard.


## Maintained mock qualification

The bundled Python mock uses the bounded asyncua dependency range and its own SDK loop. Its node IDs, control callbacks, operation limits, value histories and event archive remain the test boundary. An instance-owned attribute service preserves per-item unknown-node responses and assigns source timestamps when a client omits one, without patching library classes. Capabilities are published before the listener starts.

Full-suite qualification also exposed two maintained-mock differences. The mock now reads the structured server clock dynamically, and its instance-owned native history storage provides a continuation timestamp when the requested page leaves records. These preserve the existing diagnostic/completeness contract rather than relaxing assertions.

Migration exposed a pre-existing event-history probe error: the standard `HistoryServerCapabilities_AccessHistoryEventsCapability` is `ns=0;i=11242`, as recorded in the [SDK node identifier table](https://node-opcua.github.io/api_doc/latest/enums/node-opcua-constants.VariableIds.html#HistoryServerCapabilities_AccessHistoryEventsCapability). The shared contract now uses this ID; `i=11194` was a mock-only invention that hid independent servers' archives.


## Migration and rollback notes

No new setting is required to adopt the maintained client. Explicitly set
`OPCUA_PYTHON_BACKEND=legacy` before launching the MCP server to revert it for
this release; clear the variable or set `asyncua` to restore the default. Report
any uncovered vendor behavior with endpoint/version/security details and a
redacted reproduction, as described in the compatibility guide. The Node
runtime ignores this Python-only setting.

Both clients retain the existing certificate/key filename rule: name PEM files
`*.pem`. The existing Python security-policy baseline remains unchanged; AES
policies are still offered only on Node pending separate qualification. Standard
structures remain bounded and custom structures explicitly undecodable; no
implicit SDK text fallback becomes a contract value. SDK-specific reason text
and native timestamp precision remain declared runtime differences.

The legacy package stays installed for internal request DTOs, explicit rollback
and existing secure/independent qualification fixtures. Its network parser is
not used by the maintained client. Deprecation allowances are limited to that
remaining legacy use, rather than hiding warnings from project code or asyncua.
Retiring those uses and removing the rollback dependency is a subsequent change.
