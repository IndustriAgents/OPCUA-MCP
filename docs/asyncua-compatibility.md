# asyncua compatibility matrix (#144, step S0)

> **Status: work in progress.** Built from probes run on 2026-09-24 against
> asyncua 2.0.1 and python-opcua 0.98.13, on Python 3.13, macOS. Still to do:
> keepalive and secure-channel renewal over a long session; transfer of
> subscriptions across a reconnect; ExtensionObjects of server-defined types
> (`load_data_type_definitions`, #120/#171) against Milo; Python 3.10 runs of the
> timeout classification; the #169 conformance harness driven through an asyncua
> adapter (needs C1–C3 first); and filing the upstream issues listed at the end.
> Rows marked **not yet probed** have no evidence behind them yet.

This is the evidence #144's first acceptance criterion asks for: every OPC UA
feature the Python runtime uses today, what asyncua 2.0.1 does with it, and what
the adapter has to do about any difference. Nothing here changes the runtime;
the probes live in [`docs/asyncua-spike/`](asyncua-spike/) and their captured
output in [`docs/asyncua-spike/output/`](asyncua-spike/output/).

Verdicts: **supported** (works, nothing to adapt), **differs** (works, the
adapter must normalise something), **missing** (not in asyncua), **bug** (an
asyncua defect the adapter must work around and upstream should fix).

## Summary

asyncua 2.0.1 covers every service the Python runtime uses. The feature rows are
all *supported* or *differs*, and every difference has a mechanical fix inside
an adapter. The shared fixture tables both runtimes are held to hold on asyncua's
types unchanged: 49/49 accepted `write-coercion.json` cases encode to
byte-identical wire bytes, 24/24 `value-encoding.json` cases decode to the
expected JSON, and 1593 of 1603 unit tests pass with `import opcua` aliased to
asyncua. The 10 failures are the expected ones (below).

Three things change the plan (section [Plan changes](#plan-changes)):

1. **Transport limits are worse than the plan assumed** in asyncua, and the
   *current* Python runtime also enforces less than the contract says: neither
   `maxChunkSize` nor `maxMessageSize` is enforced on Python today.
2. **asyncua's own watchdogs act even with `auto_reconnect=False`.** A 2 s server
   stall ends the session for good, and a longer one silently replaces a
   subscription under a new id. The adapter has to own or disable both.
3. **Request timeouts arrive as a bare `Exception`**, not `TimeoutError`, and on
   Python 3.10 `asyncio.TimeoutError` is not a `TimeoutError` at all, so
   `is_connection_error()` needs a change before any asyncua call can be
   classified.

## Matrix

### Services

| Feature | Verdict | Difference / what the adapter does | Evidence |
|---|---|---|---|
| Browse + BrowseNext continuation points | supported | `uaclient.browse` / `browse_next` take the same request types. | `probe_lab.py` browse.continuation: 1200 references over 12 pages (open62541 caps at 100), release → Good. The python-opcua mock ignores the cap and answers BrowseNext with BadServiceUnsupported (`probe_services.py` browse.mock-server), so it cannot test this. |
| Batched Read, per-item status | supported | A missing node is a per-item `BadNodeIdUnknown`, not an exception, as today. | `probe_services.py` read.batched: 12 ReadValueIds → 12 DataValues. |
| Server operation limits | supported | A Read over `MaxNodesPerRead` raises a typed `BadTooManyOperations`; `operation_limits.py` chunking carries over. | `probe_lab.py` read.operation-limits (limit 40, read of 50). |
| Good subcodes (GoodLocalOverride) | supported | Status and value both returned. | `probe_services.py` read.good-subcode. |
| Batched Write + Variant typing | supported | `variant_codec.py` output encodes to identical bytes in both libraries. asyncua, like python-opcua, lets a mistyped Variant be *built* and fails only at encode, which the codec never reaches. | `probe_types.py` values.write-wire-bytes (49/49 identical); `probe_services.py` write.batched, write.variant-typing. |
| Read value encoding (`value-encoding.json`) | differs | 24/24 cases give the expected JSON through `records.variant_to_json`; the one native type change is DateTime (aware UTC, below). | `probe_types.py` values.read-json, values.native-types. |
| InputArguments | supported | Decodes to `ua.Argument` with the same fields. | `probe_services.py` methods.input-arguments. |
| Method call typing, Duration subtype | supported | Inverse HasSubtype walk resolves `i=290 → i=11`; the mock's EchoDuration received `Double:1500.0`. | `probe_services.py` methods.duration, methods.call-batched. |
| Raw history + continuation points | supported | `Node.history_read(details, cp)`; release with `ReleaseContinuationPoints=True` → Good (`history.py` unchanged). | `probe_services.py` history.raw (16 values / 8 pages), history.release; `probe_lab.py` history.raw-continuation (44 values / 2 pages). |
| Aggregate history (ReadProcessed) | supported | Average over 2 s buckets from node-opcua. | `probe_services.py` history.aggregate. |
| Event history with the contract's select clauses | supported | `history_read_events` with all 12 clauses; `events.event_record` reads asyncua's `EventFields` unchanged. | `probe_services.py` history.events (2 events). The python-opcua *server* never answers an event read that needs a continuation point, for either client, so event-history continuation points are **not yet probed** against a server that pages them. |
| Data-change subscriptions + deadband | supported | `DataChangeFilter` through `create_monitored_items`; absolute 50 suppressed every change after the first. | `probe_services.py` subscriptions.deadband: none → 5 notifications, absolute 50 → 1. Percent deadband **not yet probed** (needs an EURange node on a non-python-opcua server). |
| Event subscriptions, contract select clauses | supported | `subscribe_events(Server, evfilter=…, queuesize=1000)`; same `Event.event_fields` attribute as python-opcua. | `probe_services.py` events.subscription. |
| ConditionRefresh ordering | differs | asyncua 2.0 dispatches each notification as its own task. A **synchronous** handler keeps arrival order (Start, conditions, End in 5/5 runs); an async handler that awaits before recording reorders 5/5 runs. Keep handlers sync, as the plan says. | `probe_alarms.py` alarms.refresh-order-sync / -async. |
| A&C actions (acknowledge, confirm, comment, shelve, timed shelve, unshelve) | supported | Every contract action as a `CallMethodRequest` on the condition or its ShelvingState. `OneShotShelve` → BadInternalError is the node-opcua mock's answer for every client (pinned in `tests/e2e/test_events_e2e.py`). | `probe_alarms.py` alarms.actions. |
| Namespace array across reconnects | supported | asyncua never caches the NamespaceArray; a fresh client sees the index move 2 → 3 after `--pad-namespace`, and a stale index reads BadNodeIdUnknown. `_bind_policy_namespaces` carries over. | `probe_lab.py` namespaces.reconnect. |

### Security

| Feature | Verdict | Difference / what the adapter does | Evidence |
|---|---|---|---|
| Basic256Sha256 Sign / SignAndEncrypt + username | supported | `set_security` is now a coroutine. | `probe_security.py` security.basic256sha256; lab: both modes connect. |
| AES policies | supported | `SecurityPolicyAes128Sha256RsaOaep`, `SecurityPolicyAes256Sha256RsaPss` (no underscores). Retires the `security-policies-aes` difference at S4. | `probe_security.py` security.policies-live: 6/6 policy/mode pairs the open62541 lab offers connect. Basic128Rsa15 and Basic256 **not yet probed** live (the lab does not offer them). |
| Server certificate pinning, different certificate | supported, fail-closed | A pin to another key fails (python-opcua server: silent until the 8 s timeout; open62541: BadSecurityPolicyRejected). | `probe_security.py` security.pinning-mismatch, security.aes-pinning-mismatch. |
| Server certificate pinning, same key re-issued | supported, fail-closed | CreateSession compares the pinned DER with the server's: `UaError: Server certificate mismatch`. python-opcua does the same, so no change. | `probe_security.py` security.pinning-same-key (both libraries). |
| No pin (certificate from GetEndpoints) | supported | Same extra round trip as python-opcua. | `probe_security.py` security.unpinned. |
| No silent downgrade to None | supported | `No matching endpoints`. | `probe_security.py` security.no-downgrade. |
| ApplicationUri | differs | Default is `urn:example.org:FreeOpcUa:opcua-asyncio`, refused by a URI-checking server; the adapter sets it from the certificate as `security.py` already does. | `probe_security.py` security.application-uri. |
| Username identity, wrong password | supported | BadUserAccessDenied. | `probe_security.py` security.bad-password. |
| X.509 user identity | supported | `load_client_certificate` / `load_private_key` (coroutines). Untrusted user cert → BadIdentityTokenRejected. | `probe_security.py` security.x509-user, security.x509-user-untrusted (open62541 lab). |
| Client certificate trust | supported | Untrusted client cert → BadSecurityChecksFailed (server-side). | `probe_security.py` security.client-untrusted. |
| PEM vs DER | differs | Still decided by file suffix, or by an explicit `extension=`; bytes default to DER. The adapter sniffs `-----BEGIN` and passes `extension=`, which retires the `certificate-file-encoding` difference. | `probe_security.py` / `probe_types.py` security.pem-by-extension. |
| Loading certificates for our own checks | differs | `uacrypto.load_certificate` is async; `pinned_certificate_problem` and `certificate_application_uri` move to `cryptography.x509` (as planned). | Alias run: 4 `test_control_gate.py` + 3 `test_security_config.py` failures, all `'coroutine' object has no attribute …` ([output/alias-unit.txt](asyncua-spike/output/alias-unit.txt)). |
| User key held in memory | not yet probed | asyncua takes key content or a path; whether a key provider like node-opcua's is possible is open. | |

### Transport limits (CVE-2022-25304)

A raw hostile server in `probe_transport_limits.py` answers the Hello with an
Acknowledge the probe chooses, then streams chunks that never end; the probe
reads the client's own reassembly list. The same attack runs against
python-opcua with the runtime's `install_receive_guard()` for a baseline.

| Check | Verdict | Observed |
|---|---|---|
| Client default limits | bug | `UASocketProtocol` builds `TransportLimits(65535, 65535, 0, 0)` and `UaClient._make_protocol` passes none: chunk count and message size unlimited until the Ack says otherwise. `Client.max_chunkcount` / `max_messagesize` are only *advertised*. |
| Ack with MaxChunkCount=1024 | supported | Client held at most 1024 of 1500 chunks, then BadRequestTooLarge. |
| Ack with MaxChunkCount=0 | bug | Client advertised 1024, held all **5000** chunks. `update_client_limits` overwrites the client's limits with the server's Ack, 0 included: one Ack field reopens CVE-2022-25304. |
| Ack wider than advertised (100000) | bug | Held 3000 of 3000. The Ack widens instead of narrowing. |
| Ack MaxMessageSize=100000 | bug | Held 1,000,000 bytes. `is_msg_size_within_limit` exists but `_receive` never calls it. |
| Ack ReceiveBufferSize=2^31-1 | bug | One 8 MiB chunk accepted. The client caps incoming chunks with the Ack's `ReceiveBufferSize`, which is the *server's* receive size; Part 6 says the client's receive cap is the Ack's `SendBufferSize`. |
| Oversize chunk with an honest Ack | bug | An 8 MiB chunk is refused, but only after `data_received` has buffered the whole of it: `header.packet_size` (up to 4 GiB) is never checked before buffering. |
| `create_hello_limits` | bug (latent) | Sets `MaxMessageSize = max_chunk_count`. No caller on the client path today. |
| Hello contents | note | ReceiveBufferSize/SendBufferSize 2^31-1 by default in both libraries; with `advertise_limits` the Hello says MaxChunkCount=1024, MaxMessageSize=64 MiB. |
| **Baseline: python-opcua + runtime guard, chunk count** | supported | Held at most 1024 of 3000 whatever the Ack says, then the guard's TOO_MANY_CHUNKS. |
| **Baseline: python-opcua + runtime guard, chunk size** | bug (ours) | One 8 MiB chunk accepted. The guard counts chunks only, so **`maxChunkSize` and `maxMessageSize` in `contract/tools.json` → `transport` are not enforced on Python today**, and the contract's "largest message is 64 MiB" is not true there. Pre-existing and independent of asyncua. |

Source: `asyncua/common/connection.py` (`TransportLimits`,
`SecureConnection._receive`), `asyncua/client/ua_client.py`
(`UASocketProtocol.__init__`, `data_received`, `UaClient._make_protocol`).
Output: [output/transport.txt](asyncua-spike/output/transport.txt).

### Session, errors and values

| Feature | Verdict | Difference / what the adapter does | Evidence |
|---|---|---|---|
| Dead server | differs | Right after the server exits, a call raises `ConnectionError("Connection is closed")`, then `ConnectionError("client is disconnected")`. It is an `OSError`, so `is_connection_error()` matches it. | `probe_services.py` session.dead-server. |
| Request timeout | differs | asyncua raises a **bare `Exception("Unhandled exception while sending request…")` from `TimeoutError`**; python-opcua raises `TimeoutError`. Only the `__cause__` walk in `is_connection_error()` finds it. On 3.10, `asyncio.TimeoutError` is neither the builtin `TimeoutError` nor `concurrent.futures.TimeoutError`, so `_DEAD_SESSION_TYPES` must name it. | `probe_services.py` errors.request-timeout (live 10 s timeout); `probe_types.py` errors.timeout-type. 3.10 behaviour **not yet probed** live. |
| Supervisor liveness probe | differs | `connect()` always starts `_connection_supervisor`, which reads ServerState every `watchdog_intervall` (default 1 s) with that as the timeout. With `auto_reconnect=False`, one missed probe marks the client DISCONNECTED for good. A 2.5 s SIGSTOP of the server ended the session (TimeoutError at 2.0 s). python-opcua has no such probe. | `probe_lab.py` keepalive.stall-default. |
| Stale-subscription watchdog | differs | Also started unconditionally. After 9 s without publish responses (200 ms × 20 keep-alives × 1.5) it re-created the subscription **under a new id (1 → 2)** without telling the caller. | `probe_lab.py` keepalive.stale-subscription. |
| Notification dispatch / overflow policy | differs | Handler mode schedules one task per notification with no bound; the queue and its `OverflowPolicy` (incl. DISCONNECT) apply to iterator mode only. | Source: `common/subscription.py` `_deliver`. |
| Secure-channel renewal, long sessions | not yet probed | Renews at 75 % of `secure_channel_timeout` (source). | |
| StatusCode names | supported | All 14 `deadSession.statusCodeNames` exist in both with the same numbers. asyncua has 28 more names; only `BadSempahoreFileMissing` (a typo) is python-opcua-only. No renumbering. Unknown codes are named `Bad` in both. | `probe_types.py` status.*. |
| Status exceptions | differs | Both raise a per-code subclass of `UaStatusCodeError` with `.code`; the text drops python-opcua's quotes. Classification by code is unaffected; the reason text is the declared `library-reason-text` difference. | `probe_types.py` errors.status-exception. |
| DateTime | differs | asyncua decodes aware UTC (python-opcua naive UTC), including the 1601-01-01 "no timestamp" value. A naive datetime written through asyncua is taken as UTC, so writes agree. The port normalises reads to aware UTC. | `probe_types.py` datetime.aware, datetime.zero; `read.batched` / `history.raw` report `tzinfo=UTC`. |
| Dependency deprecation warnings | supported | 0 DeprecationWarnings from asyncua building a DataValue and encoding a DateTime; python-opcua raises `utcnow()` ones. | `probe_types.py` datetime.deprecations. |
| ExtensionObjects (#171) | differs | asyncua decodes namespace-0 structures into dataclasses whose `str()` keeps the fields (`Range(Low=-50.0, High=250.0)`); python-opcua's `str()` drops them. A generic Part 6 JSON encoder can walk `dataclasses.fields()`. `records.py` still falls back to `str()`, so #171 still needs its fix. Unknown types stay `ExtensionObject` with the raw body. | `probe_types.py` values.extension-object*; `probe_lab.py` values.structure-live (open62541 Range and EUInformation). |
| API renames | differs | `Node.server` → `Node.session`, `get_attributes` → `read_attributes`, `get_value` → `read_value`; `subscribe_events` keeps `evfilter` / `queuesize`. | `probe_types.py` api.renames. |
| Dependencies | differs | Requires Python ≥3.10 and LGPL-3.0+, both unchanged. Adds `aiosqlite`, `pyopenssl`, `sortedcontainers`, `typing-extensions`, and `wait-for2` on <3.12; `cryptography` becomes mandatory; drops `lxml`. Affects the wheel, the PyInstaller executable and the SBOM. | `asyncua-2.0.1.dist-info/METADATA`. |

### The runtime's pure code on asyncua's `ua` module

`opcua_alias.py` is a pytest plugin that makes `import opcua` load `asyncua`.
It is not an adapter; it measures which of today's translations survive the swap.
The whole unit suite: **1593 passed, 10 failed, 37 skipped** (the same 37 skip
without it).

- 7 are the async `uacrypto` loaders above.
- 2 are `test_transport_limits.py` reaching into python-opcua's `ua.SecurityPolicy`,
  which is expected: the guard is replaced, not ported.
- 1 (`test_install_parity.py[python]`) comes from running under `uv run --with`,
  not from asyncua.

## Plan changes

1. **Transport limits: wrap, fix Hello, bound before buffering, and fix Python
   today.** The plan's four asyncua findings are confirmed (below). There is one
   more: the chunk cap comes from the Ack's *ReceiveBufferSize*, and
   `packet_size` is not checked before a chunk is buffered. The adapter needs its
   own `UASocketProtocol` subclass or wrapper that:
   - installs the contract's limits;
   - lets an Ack only narrow them;
   - checks `packet_size` against `maxChunkSize` in `_process_received_data`
     before buffering;
   - enforces `maxMessageSize` on reassembly.

   Separately, **the current python-opcua runtime does not enforce `maxChunkSize`
   or `maxMessageSize` at all**. That is worth a bug of its own now, not at S1:
   `SECURITY.md` and the contract say 64 MiB.
2. **Neutralise asyncua's watchdogs.** Keeping `auto_reconnect=False` is not
   enough. The adapter should either stop the supervisor's probe (a large
   `watchdog_intervall`, or cancel `_supervisor_task`) and the stale-subscription
   watchdog (`_stale_watchdog_task`), or adopt them on purpose and declare the
   behaviour. As shipped, a 2 s PLC stall ends the session, and a subscription id
   the runtime has recorded changes underneath it. Add both to the S2 negative
   tests.
3. **Error classification before C2.** Add `asyncio.TimeoutError` to
   `_DEAD_SESSION_TYPES`, and keep the `__cause__` walk: asyncua hides the timeout
   behind a bare `Exception`. Add a 3.10 CI cell for it.
4. **#171 gets cheaper on asyncua.** Structure fields survive decoding, so the
   ExtensionObject JSON encoding can be written once against dataclasses.
5. **Mocks.** The python-opcua mock cannot test BrowseNext or paged event history,
   so the open62541 lab (#169) should be in the S2 dual-CI legs, not only the
   conformance job.

### The plan's asyncua risks, checked

| Claim in plan-141-144 §2 | Result |
|---|---|
| Client default `TransportLimits(65535,65535,0,0)` = unlimited | **Confirmed** (source and live). |
| `update_client_limits` overwrites with the server's Ack, incl. 0 | **Confirmed** live: Ack 0 → 5000 chunks held; Ack 100000 → widened. |
| `create_hello_limits` sets MaxMessageSize = max_chunk_count | **Confirmed**, but it is **dead code** on the client path: `Client` sends its own `max_messagesize` / `max_chunkcount`. Latent, not live. |
| MaxMessageSize never enforced on receive | **Confirmed** live. |
| AES policies exist (class names differ) | **Confirmed**; 4/4 AES policy/mode pairs connect to open62541. |
| `uacrypto` sniffs PEM by extension unless `extension=` | **Confirmed**; bytes default to DER too. |
| `subscribe_events` signature; `Node.server` → `Node.session`; `get_attributes` → `read_attributes` | **Confirmed.** |
| 2.0 dispatches notifications as tasks; use sync handlers | **Confirmed** live: an async handler reorders ConditionRefresh, a sync one never did. |
| Never enable DISCONNECT overflow | Holds, but it only exists in iterator mode; handler mode has no queue at all. |
| Keep `auto_reconnect=False` | Necessary, **not sufficient** (plan change 2). |
| New deps pyopenssl, aiosqlite, pytz | pyopenssl and aiosqlite confirmed; **pytz is not new** (python-opcua already requires it); sortedcontainers, typing-extensions and wait-for2 are new; lxml goes away. |

## Upstream bugs to file (not filed)

For the maintainer to file at `FreeOpcUa/opcua-asyncio`. Each reproduces with
`probe_transport_limits.py`.

1. **Client lets the server's Acknowledge widen or remove its receive limits.**
   `SecureConnection.receive_from_header_and_body` → `TransportLimits.update_client_limits`
   copies Ack.MaxChunkCount / MaxMessageSize / buffer sizes verbatim. Repro: a server
   answering Hello with `Acknowledge(MaxChunkCount=0)`, then streaming MSG chunks
   with ChunkType `C` on channel 0; the client buffers every chunk. Expected: take
   the minimum of the client's own limit and the Ack's, treating 0 as "no
   server limit", not "no client limit".
2. **Client never enforces MaxMessageSize on receive.** `_receive` checks chunk
   count only; `is_msg_size_within_limit` has no caller. Repro: Ack
   MaxMessageSize=100000, then 1000 chunks of 1000 bytes: all are held.
3. **Client chunk-size cap uses the Ack's ReceiveBufferSize instead of
   SendBufferSize.** `update_client_limits` sets `max_recv_buffer = msg.ReceiveBufferSize`.
   Repro: Ack ReceiveBufferSize=2^31-1, then one 8 MiB chunk: accepted.
4. **`UASocketProtocol` buffers a whole chunk before any size check.**
   `_process_received_data` waits for `header.body_size` bytes (up to 4 GiB) with
   `receive_buffer + data` concatenation before `_receive` compares `packet_size`.
   Repro: an honest Ack, then one MSG header claiming 8 MiB.
5. **`Client` passes no `TransportLimits` to `UASocketProtocol`.**
   `UaClient._make_protocol` constructs it without `limits=`, so the default
   `TransportLimits(65535, 65535, 0, 0)` applies and there is no public way to set
   receive limits.
6. **`TransportLimits.create_hello_limits` copies max_chunk_count into
   MaxMessageSize** (typo). Repro: `TransportLimits(65535, 65535, 7, 999999).create_hello_limits(ua.Hello()).MaxMessageSize == 7`.
7. **Request timeouts surface as a bare `Exception`.** `UASocketProtocol.send_request`
   re-raises any non-`UaError` as `Exception("Unhandled exception while sending
   request to OPC UA server")`, hiding `TimeoutError`. Repro: a server that never
   answers one request (the python-opcua server's paged event HistoryRead does
   this).
8. **(Question, not a bug)** Should the connection supervisor's ServerState probe
   and the stale-subscription watchdog run when `auto_reconnect=False`? Today a
   probe timeout of `watchdog_intervall` permanently disconnects a
   non-reconnecting client.

## Reproducing

```sh
uv sync --all-packages
(cd packages/mock-server-aggregate && npm ci)
(cd packages/mock-server-alarms && npm ci)
# optional, for the AES / X.509 / browse-paging / namespace rows (#169's lab):
#   git show origin/midhunxavier/conformance-matrix:compatibility/labs/open62541/build.sh > build.sh
#   (and lab_server.c alongside it), then  sh build.sh /tmp/o62541
export OPEN62541_LAB_SERVER=/tmp/o62541/lab_server

cd docs/asyncua-spike
uv run --no-sync --with asyncua==2.0.1 python probe_types.py
uv run --no-sync --with asyncua==2.0.1 python probe_transport_limits.py
uv run --no-sync --with asyncua==2.0.1 python probe_services.py
uv run --no-sync --with asyncua==2.0.1 python probe_alarms.py
uv run --no-sync --with asyncua==2.0.1 python probe_security.py
uv run --no-sync --with asyncua==2.0.1 python probe_lab.py
cd ../../tests && PYTHONPATH=../docs/asyncua-spike \
  uv run --no-sync --with asyncua==2.0.1 python -m pytest -p opcua_alias -q unit/
```

Each probe starts the mocks it needs on free ports and stops them on exit. The
python-opcua mock is `packages/mock-server`, the aggregate and alarm mocks are
the node-opcua ones, and the secured mock is `tests/fixtures/secure_opcua_server.py`.
Probe verdicts are the probe's own reading. Where a row above differs from the
raw line, the row gives the reason.
