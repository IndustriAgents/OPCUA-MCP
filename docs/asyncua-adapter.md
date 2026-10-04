# Maintained Python adapter migration

The production client still uses python-opcua. Issue [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) remains open until the maintained adapter passes the feature, security, reconnect and distribution gates. The nine application ports extracted in #141 provide its contract boundary.

The first implemented slice is `adapters/asyncua_transport.py`, qualified against asyncua 2.0.1 on native wire frames. Its dependency range is `>=2.0.1,<2.1`; widening this minor ceiling requires requalifying the receive and lifecycle hooks. It does not monkey-patch library classes or select a new production backend. `adapters/asyncua_client.py` composes that protocol per client and cancels autonomous watchdog tasks before they run, preserving native secure-channel renewal. Live connection/read and channel-renewal tests qualify this construction; it is not yet wired to MCP feature adapters.

## Compatibility and qualification matrix

| Required feature | Maintained adapter evidence / remaining work |
|---|---|
| Browse and continuation release | Application fake-port characterization exists; wire/client integration pending |
| Batch read/write and Variant typing | Application characterization exists; native shared-value/request codec tests pass; live whole-batch control integration pending |
| Method InputArguments | Application characterization exists; native Argument value encoding passes; live metadata/type resolution pending |
| Raw and aggregate history | Application characterization exists; bounded paging/release integration pending |
| Event history | Application characterization exists; native paged event history pending |
| Data-change subscription and deadband | Application characterization exists; native monitored-item/filter integration pending |
| Alarms & Conditions | Application characterization exists; refresh ordering and native action integration pending |
| Security policies, exact pin, username and X.509 users | Native handshake and negative authentication tests pending |
| Namespace URI and reconnect generation | Client lifecycle integration and namespace rebinding tests pending |
| Message/chunk bounds | Native framing tests cover hostile split headers, local receive caps, count/byte reassembly caps, exact boundaries and memory release |
| Negotiated receive/send limits | Native Ack tests prove correct buffer direction; zero or enlarged peer limits never widen local caps |
| Keepalive, renewal and subscription reconnect | The qualification client disables autonomous reconnect/subscription watchdogs and renews a native secure channel without replacing its session; subscription reattachment and longer/vendor sessions remain pending |
| Timestamps, status names and structured values | All 32 shared native value cases pass, including timestamps, StatusCodes, standard ExtensionObjects, opaque values and bounded cycles |
| Python 3.10/current | Foundation native/live tests pass on Python 3.10 and current Python; full adapter matrix remains pending |
| Dual backend CI and rollback | Temporary comparison matrix and one-release rollback selection pending |
| Packaged wheel and executable | Both maintained and rollback distribution paths require smoke qualification |

## Receive boundary

The protocol retains at most one bounded chunk while collecting split network callbacks. It reads the eight-byte header first, then refuses an oversized chunk or an over-budget secure message before retaining its body. It feeds complete chunks to asyncua's native parser; native sequence, request, channel, security token, signature and encryption checks remain active. A refusal clears partial and assembled buffers, fails pending requests and closes the channel. Application recovery continues to own whether a later logical request may reconnect or resend.

This boundary is necessary because [asyncua 2.0.1's transport](https://github.com/FreeOpcUa/opcua-asyncio/blob/v2.0.1/asyncua/common/connection.py) overwrites local limits from the peer Ack and checks chunk count without a cumulative wire-byte cap; its [socket protocol](https://github.com/FreeOpcUa/opcua-asyncio/blob/v2.0.1/asyncua/client/ua_client.py) waits for the advertised body before checking the chunk size. The repository's earlier [compatibility spike PR #174](https://github.com/IndustriAgents/OPCUA-MCP/pull/174) contains additional observations that still require qualification against the completed #141/#157 code.

Run `uv run pytest -q tests/unit/test_asyncua_transport.py` to exercise native frames, including byte-at-a-time Acks, headers with no body, multiple chunks in one callback, repeated complete messages and Hello timeout cleanup. These tests do not establish full client or vendor compatibility.

## Native value boundary

`adapters/asyncua_values.py` converts fields of locally constructed request DTOs into maintained native types. The maintained library owns binary encoding and network response parsing; the legacy DTOs remain an internal compatibility boundary for rollback. Nonempty request checks retain node IDs, typed method/write values, aggregate parameters and opaque continuation points. Standard native structures use library-known class identities and bounded field traversal without evaluating annotations. Unknown/custom structures remain explicitly undecodable. `adapters/asyncua_services.py` owns a private maintained SDK loop behind the existing async application ports. Live core checks cover engineering metadata, typed writes, derived method arguments, raw history and filtered subscriptions; native failures retain numeric status/cause classification and failed connection attempts stop their owned loop. End-to-end MCP backend selection, security, event/alarm and reconnect qualification, and dual-backend CI are still required.
