# Maintained Python adapter migration

The production client still uses python-opcua. Issue [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) remains open until the maintained adapter passes the feature, security, reconnect and distribution gates. The nine application ports extracted in #141 provide its contract boundary.

The first implemented slice is `adapters/asyncua_transport.py`, qualified against asyncua 2.0.1 on native wire frames. Its dependency range is `>=2.0.1,<2.1`; widening this minor ceiling requires requalifying the receive and lifecycle hooks. It does not monkey-patch library classes or select a new production backend.

## Compatibility and qualification matrix

| Required feature | Maintained adapter evidence / remaining work |
|---|---|
| Browse and continuation release | Application fake-port characterization exists; wire/client integration pending |
| Batch read/write and Variant typing | Application characterization exists; native codecs and whole-batch control gate pending |
| Method InputArguments | Application characterization exists; native metadata/type resolution pending |
| Raw and aggregate history | Application characterization exists; bounded paging/release integration pending |
| Event history | Application characterization exists; native paged event history pending |
| Data-change subscription and deadband | Application characterization exists; native monitored-item/filter integration pending |
| Alarms & Conditions | Application characterization exists; refresh ordering and native action integration pending |
| Security policies, exact pin, username and X.509 users | Native handshake and negative authentication tests pending |
| Namespace URI and reconnect generation | Client lifecycle integration and namespace rebinding tests pending |
| Message/chunk bounds | Native framing tests cover hostile split headers, local receive caps, count/byte reassembly caps, exact boundaries and memory release |
| Negotiated receive/send limits | Native Ack tests prove correct buffer direction; zero or enlarged peer limits never widen local caps |
| Keepalive, renewal and subscription reconnect | Lifecycle ownership and long-session tests pending; library watchdogs must not create an unaudited retry path |
| Timestamps, status names and structured values | Existing shared fixtures must pass with maintained native types |
| Python 3.10/current | Foundation tests run on current Python; floor qualification follows before merging |
| Dual backend CI and rollback | Temporary comparison matrix and one-release rollback selection pending |
| Packaged wheel and executable | Both maintained and rollback distribution paths require smoke qualification |

## Receive boundary

The protocol retains at most one bounded chunk while collecting split network callbacks. It reads the eight-byte header first, then refuses an oversized chunk or an over-budget secure message before retaining its body. It feeds complete chunks to asyncua's native parser; native sequence, request, channel, security token, signature and encryption checks remain active. A refusal clears partial and assembled buffers, fails pending requests and closes the channel. Application recovery continues to own whether a later logical request may reconnect or resend.

This boundary is necessary because [asyncua 2.0.1's transport](https://github.com/FreeOpcUa/opcua-asyncio/blob/v2.0.1/asyncua/common/connection.py) overwrites local limits from the peer Ack and checks chunk count without a cumulative wire-byte cap; its [socket protocol](https://github.com/FreeOpcUa/opcua-asyncio/blob/v2.0.1/asyncua/client/ua_client.py) waits for the advertised body before checking the chunk size. The repository's earlier [compatibility spike PR #174](https://github.com/IndustriAgents/OPCUA-MCP/pull/174) contains additional observations that still require qualification against the completed #141/#157 code.

Run `uv run pytest -q tests/unit/test_asyncua_transport.py` to exercise native frames, including byte-at-a-time Acks, headers with no body, multiple chunks in one callback, repeated complete messages and Hello timeout cleanup. These tests do not establish full client or vendor compatibility.
