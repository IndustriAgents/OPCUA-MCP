# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation
- **The one-endpoint-per-process ceiling is now stated where someone meets it**
  (#88). `OPCUA_SERVER_URL` is read once, every tool targets it, stdio is the only
  transport, and each MCP client opens its own OPC UA session — which on equipment
  where sessions are licensed is a cost per client per endpoint. README.md says
  so in the overview.
- **#15's reopen condition had been met and nobody noticed.** `ROADMAP.md` set
  multiple endpoints aside until "node-ID canonicalisation and URI-based
  allowlists have landed"; both shipped in 0.4.0. The roadmap now records that the
  condition lapsed, what the *remaining* reason is (a second endpoint needs an
  endpoint registry, per-endpoint policy, session pooling and a multi-client
  transport — one piece of work, of which #14 is half), and a reopen condition
  that can actually be observed.

### Fixed
- **The Python server advertised schemas that were not the contract's** (#81).
  Its `tools/list` carried the schema `MCPServer` derives from each function
  signature, which has no per-argument descriptions and no nested structure:
  `write_opcua_nodes` offered `nodes` as "an array of object" against a contract
  naming `node_id`, `value` and the fifteen legal `data_type` spellings. Tool
  descriptions matched between the runtimes; the parameter documentation a model
  needs in order to call the tool did not. Both servers now advertise the
  contract's own schema, and the parity test compares the whole thing instead of
  top-level property names.
- **The Node server validated nothing** (#82). The low-level MCP `Server` does
  not check `arguments` against the advertised `inputSchema`, and the dispatcher
  cast straight off the wire, so a malformed call reached node-opcua as whatever
  the client sent. Both runtimes now check the contract's schema before the
  policy layer sees the call, through one shared table of cases
  (`tests/fixtures/argument-validation.json`) that both unit suites run.
- **The same failure was worded three ways** (#86). The Node server prefixed
  every message with `Error: `; the Python SDK prefixes a `ToolError` raised
  inside a tool body with `Error executing tool <name>: `; neither was tested,
  because the differential suite compared substrings. Every message now comes
  from `contract/tools.json` -> `errors`, neither runtime adds a frame, and a
  table of failing calls is driven through both with the full text compared.
- **The Node server never re-checked capability gating at invocation time.** A
  client holding a `tools/list` from when the server still reported
  HistoricalAccess could call `read_opcua_history` against one that does not, and
  reach node-opcua instead of the refusal the Python runtime gives. Found by the
  new failure table.
- **`read_opcua_history` reported a missing `start_time` as a failed read** on
  the Node runtime — wrapped as "Failed to read history of node …" for a request
  that never reached the OPC UA server.

- **`tools/list` waited on the network, once per call** (#83). It opened a
  connection before probing capabilities, and `connect` holds its lock across the
  whole backoff loop — so against an unreachable plant every catalogue request
  paid the full reconnect budget (7s by default, 32s with
  `OPCUA_RECONNECT_MAX_RETRY=-1`) and serialised every concurrent tool call
  behind it. Clients list at session start, which is when a plant that is down is
  most likely to be down. Capabilities are now probed where they can change —
  once at startup and again on every reconnect — and `tools/list` does no network
  I/O at all.
- **A request served during Node's startup could poison the capability cache.**
  The warm-up ran after the transport was connected, so a `tools/call` landing
  mid-probe found no session yet, cached "this server supports nothing", and
  refused a history read against a server that advertises HistoricalAccess for
  the rest of the process. The warm-up now completes before the first request, as
  the Python lifespan has always done, and "no session yet" is no longer cached
  as an answer.
- **Security: CVE-2022-25304, unbounded chunk reassembly in `python-opcua`.**
  An OPC UA message may be split across chunks, and `python-opcua` appends each
  one to a list with nothing counting it, so a server that never terminates the
  message exhausts the client. The advisory has no patched version and will not
  get one — the library is unmaintained and the advisory names `asyncua` too.
  Both runtimes now advertise `MaxChunkCount`/`MaxMessageSize` in the OPC UA
  Hello (python-opcua's defaults are `0`, i.e. unlimited) and enforce them on
  receipt from one shared bound in `contract/tools.json` -> `transport`:
  node-opcua natively, and the Python runtime by wrapping
  `SecureConnection._receive`. See SECURITY.md for the residual risk.
- **The audit trail's lines could not be tied together** (#87). Each control call
  writes `allowed` and then `completed`/`failed`, and nothing linked them; two
  concurrent writes to the same node were not distinguishable by content at all.
  Every line now carries a `call_id`.
- **A raw history read had no bound** (#85). `num_values: 0` meant "every reading
  in the range" — against a node historised at 100ms, the same request that never
  returns that the browse caps were added to prevent. It now means "as many as
  allowed" (`limits.maxHistoryValues`, 5000), and a read that stops at the cap
  says so in a trailing notice rather than returning a short list that reads as
  complete. A batch read is capped at `limits.maxNodesPerRead` (500) and refused
  rather than truncated, and `subscribe_opcua_nodes` counts against
  `limits.maxSubscriptions` (200) because it asks a PLC for one subscription per
  node.
- **The artifact smoke fixtures could not run on Windows**, the third thing the
  cross-platform job found. They invoked `npm`/`npx` as bare names, which
  `subprocess` cannot resolve to `npm.cmd` without a shell (`[WinError 2]`), and
  they looked for `lib/pythonX.Y/site-packages` in a venv where Windows puts
  `Lib/site-packages`. The fixtures already called `shutil.which` and threw the
  answer away; they now use it.
- **Forty-six test files read the contract at the system locale**, also found by
  the new cross-platform job. `Path.read_text()` defaults to the system encoding,
  which on Windows is cp1252 — so every em dash and ellipsis in
  `contract/tools.json` came back as a replacement character and the description
  comparisons failed. The product has always passed `encoding="utf-8"`
  explicitly, with a comment saying why; the tests never did, and on macOS and
  Linux the locale happens to be UTF-8 so nothing noticed. Fixed everywhere, with
  a unit test that fails if a bare read reappears.
- **`isEphemeralInstall` missed npx caches spelled with forward slashes on
  Windows** — found by the new cross-platform CI job on its first run. It split
  the path on the *host's* separator, so a Windows path written with `/` (which
  Windows accepts, and which `process.argv[1]` may well carry) was one unsplit
  segment that matched nothing: `--install` would then write the npx cache path
  into a client config as though it were a real install, and the entry would
  break the next time the cache was pruned. It now splits on either separator,
  which is what the Python half already got for free from `Path(...).parts`.
- **Security: `tmp` path traversal (GHSA-ph9p-34f9-6g65, GHSA-52f5-9888-hmc6).**
  A dev-only transitive dependency, reached through
  `@anthropic-ai/mcpb` → `@inquirer/prompts` → `@inquirer/editor` →
  `external-editor` → `tmp@0.0.33`, whose vulnerable `tmpNameSync` is the API
  `external-editor` calls. No version bump fixes it — `external-editor` pins
  `^0.0.33` at its own latest — so it is pinned with an npm `overrides` entry to
  `^0.2.6`. `npm audit` is clean.

### Changed
- Behavioural parity is now driven by shared tables rather than by hand-mirrored
  code (#90), extending the pattern `tests/fixtures/value-encoding.json`
  established: one table for argument validation, one for failure wording.
- Neither runtime sends `notifications/tools/list_changed`, and
  `docs/architecture.md` now says so beside the same decision for
  `notifications/resources/updated`, with the SDK-generation reason (#84). The
  catalogue is re-listable at any time and converges without it.
- New shared homes for what both runtimes must agree on: `contract/tools.json` ->
  `limits` (bounds) and `notices` (messages added beside a result, including the
  dropped-events sentence that was two hand-mirrored literals), read by
  `limits.py` / `limits.ts` and `notices.py` / `notices.ts`, with
  `tests/fixtures/history-limits.json` driving both.
- **CI now runs on Windows and macOS** (#89), which nothing in it touched before.
  A new `cross-platform` job runs the unit suites and the artifact smoke tests on
  both, covering the per-OS branches in `install.py` (the `%APPDATA%` config path
  among them) and `isEntryPoint()`'s `realpathSync` comparison, which Windows
  junctions do not behave like POSIX symlinks under. It is deliberately not yet a
  required status check.
- Dependency bumps, superseding Dependabot PRs #66, #67 and #68: `@types/node`
  26.5.0 → 26.6.0, `pyinstaller` 6.22.2 → 6.22.3, `ruff` 0.16.6 → 0.16.8.
- Further dependency bumps, superseding Dependabot PRs #98–#102:
  `node-opcua-client` 2.183.1 → 2.184.8 and `node-opcua-crypto` 5.10.1 → 5.11.0
  (both runtime, so the full end-to-end suite is what clears them),
  `@types/node` → 26.6.2, `prettier` → 3.9.8, and in `release.yml`
  `actions/upload-artifact` v4 → v7 with `actions/download-artifact` v4 → v8.

## [0.4.1] — 2026-09-18

0.4.0 was tagged but never reached npm or PyPI: its publish workflow failed the
verify job it runs before publishing, and skipped both registry jobs. The GitHub
release and its downloadable artifacts were built and are correct — the failure
was a flaky *test*, not a defect in either server. This release is 0.4.0 plus the
fix for that test, so **0.4.1 is the first published release of the 0.4 line**
and the 0.4.0 notes below describe what is in it.

Tags in this repository are immutable by ruleset, which is why this is a new
version rather than a re-tag.

### Fixed
- **Three end-to-end tests raced the mock's simulation loop.** Every actuator in
  the bundled mock is republished from the simulation's own state once a second,
  so a test that wrote one and read it back was racing a timer. It passed
  locally, passed in CI, and then failed the 0.4.0 release verify — reading back
  `50.0` where it had written `31.5`, which is the actuator's default.

  The mock had no node that could be written and read back deterministically,
  which is a gap in a test fixture for an OPC UA server. It now has two
  (`Scratch/ScratchDouble`, `Scratch/ScratchBoolean`) that nothing simulates, and
  the three tests use them. `test_mock_server_e2e.py` pins both halves of the
  contract — an actuator must revert, a scratch node must not — so re-pointing a
  write test at an actuator fails there, with an explanation, rather than
  intermittently somewhere else.

  Test fixture only; no change to either shipped server.

## [0.4.0] — 2026-09-18

Two correctness bugs, two security features, and a tool surface that went from
seventeen tools to thirteen. The consolidation is breaking; the migration table
is under **Changed — BREAKING** below.

The thread running through all of it: the contract now pins *behaviour*, not only
interface. Ten of the seventeen tools declared no result shape, and every
divergence between the two runtimes lived in exactly that gap — so the fix for
the bugs and the reason for the merge are the same fix.

### Changed — BREAKING

- **The tool surface is consolidated from seventeen tools to thirteen.** Four
  single/batch pairs, and the raw/aggregate history pair, become one tool each.
  Reading one node and reading fifty is the same request with a longer list, so
  it is now the same tool — and, more to the point, the same *code path*.

  | Removed | Use instead |
  |---|---|
  | `read_opcua_node` | `read_opcua_nodes` with `node_ids: [id]` |
  | `read_multiple_opcua_nodes` | `read_opcua_nodes` (`node_ids` unchanged) |
  | `write_opcua_node` | `write_opcua_nodes` with `nodes: [{node_id, value}]` |
  | `write_multiple_opcua_nodes` | `write_opcua_nodes` (`nodes_to_write` → `nodes`) |
  | `browse_opcua_node_children` | `browse_opcua_nodes` (same `node_id`; `depth` defaults to 1) |
  | `get_all_variables` | `browse_opcua_nodes` with `depth`, `node_class: "Variable"`, `include_values: true` |
  | `read_history_opcua_node` | `read_opcua_history` (same arguments) |
  | `read_aggregate_opcua_node` | `read_opcua_history` with `aggregate_function` |
  | `subscribe_opcua_node` | `subscribe_opcua_nodes` with `node_ids: [id]` |
  | `unsubscribe_opcua_node` | `unsubscribe_opcua_nodes` with `subscription_ids: [id]` |

  No deprecated aliases. Keeping the old names alive would mean keeping the
  second code path alive, and that path is precisely the problem: each merged
  pair was the same operation written twice per runtime — four copies — which is
  where the two most recent correctness bugs actually lived. #75 was a missing
  browse continuation-point drain that reached two tools independently because
  each browsed separately; #76 was a batch read reporting failure as success on
  one runtime while its single-node sibling did not.

  `browse_opcua_nodes` also absorbs what #11 asked two further tools for, so the
  count goes down rather than up: `browse_path` resolves `/Objects/Plant/Temp`
  to a node id (with `depth: 0`, that is all it does), and `name_filter`
  searches browse names. Both reuse the one traversal.

- **Every tool now declares a result shape, and returns records rather than
  prose.** Ten of the seventeen tools declared `resultShape: null`, and for those
  the output format, error wording and defaults were two hand-written copies that
  no test compared — the parity suite could prove the two servers *advertise* the
  same thing, never that they *do* the same thing. Four confirmed divergences
  lived in exactly that gap.

  Six shapes are new (`nodeValues`, `nodeRefs`, `writeResults`, `methodResult`,
  `eventSubscription`, `acknowledgement`), bringing every tool under one. Callers
  parsing text will need to change:

  | Tool | Was | Now |
  |---|---|---|
  | read | `Node ns=2;i=3 value: 26.9` | `{node_id, value, data_type, status, source_timestamp, server_timestamp}` |
  | write | `Successfully wrote 80 to node …` | `{node_id, status, error}` |
  | browse | `Children of ns=2;i=1: [{…}]` (Python `repr`, Node JSON) | `{nodes: [...], truncated, inspected}` |
  | discovery | `Found 22 variables: - Name: …` | the same `nodeRefs` object |
  | method call | `Method call successful. … Result: True` | `{object_node_id, method_node_id, status, outputs}` |
  | unsubscribe | `Unsubscribed sub-1 from node … after 4 changes` | the subscription record as it was when cancelled |
  | subscribe_events | `Subscribed to events from node …` | `{node_id, severity_min, buffer_size, replaced}` |
  | acknowledge_alarm | `Acknowledged alarm ns=1;i=1002 (event …)` | `{event_id, condition_id, status}` |

  This closes [#8](https://github.com/IndustriAgents/OPCUA-MCP/issues/8): a reading
  now carries its data type, its OPC UA status and both timestamps, because the
  quality and the age are what decide whether a value can be acted on and a bare
  number carries neither.

  **Read values now go through the shared codec.** `read_opcua_node` and
  `get_all_variables` stringified natively on both runtimes and so diverged by
  construction — a Boolean rendered `true` against `True`, an Int64 as
  node-opcua's `[high, low]` pair against a plain int. The fixture that exists to
  prevent exactly that (`tests/fixtures/value-encoding.json`) only ever fed the
  history family; the read path was outside its reach. It no longer is.

- **`write_opcua_nodes` accepts an explicit `data_type`**, closing
  [#9](https://github.com/IndustriAgents/OPCUA-MCP/issues/9). Without it each node
  is read first to learn its type, which costs a round trip and cannot work for a
  **write-only** node — reading it is exactly what such a node refuses. A batch
  that declares every type sends no reads at all.

- **`call_opcua_method` converts arguments to the types the method declares**,
  closing [#10](https://github.com/IndustriAgents/OPCUA-MCP/issues/10). It parsed
  every argument float → int → string and then forced `Double` or `String`, so a
  method expecting a `Boolean` or an `Int32` was called with the wrong type and
  either failed or — worse — acted on a coerced value. The declared types come
  from the method's own `InputArguments`; the old heuristic survives only as the
  fallback for a method that publishes none.

- **Capability gating moved from the tool to the argument** where the two history
  tools merged. `read_opcua_history` is offered when the server reports
  historical access *or* aggregates — either makes some form of it usable — and
  `aggregate_function` appears only with the latter, its description naming that
  server's own function list. A server offering only aggregates is no longer left
  with no history tool at all.

- **Traversal bounds live in the contract** (`traversal`), not as literals in
  both runtimes. A browse that stops at `max_nodes` reports `truncated: true`
  rather than trailing prose, so a prefix of the address space can no longer pass
  for all of it.

### Added
- **Server-certificate verification** (#45). `OPCUA_SERVER_CERT` pins the
  certificate the OPC UA server must present. Without it — the behaviour up to
  now — the certificate is taken from the endpoint description and used to
  encrypt to, which protects against passive eavesdropping but not against
  whoever managed to answer: DNS, ARP, a compromised switch or a mistyped
  endpoint all reach that. Both runtimes now say so on stderr on an otherwise
  fully secured connection, because `policy=Basic256Sha256 mode=SignAndEncrypt`
  reads like the connection is safe and the one thing it does not establish is
  who is on the other end. Pinning silences the warning and adds
  `server-cert=pinned` to the startup summary.

  Pinning it on an unsecured channel is refused rather than ignored: with
  `policy=None` the server presents no certificate at all, so the setting would
  verify nothing while reading, in a config file, exactly like protection.

  The test that matters is the negative one — a valid, well-formed impostor
  certificate carrying the right ApplicationUri is refused by both runtimes,
  with the positive case as its control. On the Node side this promotes
  `node-opcua-crypto` from a transitive to a direct dependency.
- **X.509 certificate-based user authentication** (#7). `OPCUA_USER_CERT` and
  `OPCUA_USER_KEY` authenticate the *user* by certificate instead of
  username/password. Deliberately named apart from `OPCUA_CLIENT_CERT`: that one
  is the application's identity and secures the channel, this one is the user's
  and is what the server checks against its user list — a different key pair,
  and conflating the two is the obvious way to get this wrong. Configuring both
  a user certificate and a username is refused at startup, because a session
  carries one identity and silently picking one would leave the operator
  believing the other was in force. The Node runtime signs the challenge through
  `keyOperations`, so the private key never becomes a string in the process.

- **Connection resilience: auto-reconnect, keep-alive and backoff** (#18). Neither
  server needs restarting when the OPC UA server does. A dropped or refused
  connection is retried with exponential backoff, configurable through four
  variables that mean the same thing on both runtimes —
  `OPCUA_RECONNECT_INITIAL_DELAY_MS`, `OPCUA_RECONNECT_MAX_DELAY_MS`,
  `OPCUA_RECONNECT_MAX_RETRY` (`-1` for unlimited) and `OPCUA_SESSION_TIMEOUT_MS`,
  which also sets the keep-alive period. The waits they produce are pinned
  against each other in `tests/unit/test_reconnect.py`, and both servers print
  what is in force on startup.

  The two runtimes get there from opposite directions: node-opcua repairs its own
  channel and re-activates the same session, so the Node side follows its
  `connection_lost` / `connection_reestablished` / `close` events and knows when
  the library has given up; python-opcua has no reconnection at all, so the
  Python side owns the whole backoff loop and builds a fresh client per attempt
  — a restarted server may be presenting a new certificate.

  Read and write paths transparently re-establish a dead session and retry once,
  but only for tools the contract declares idempotent: `call_opcua_method` and
  `acknowledge_alarm` get the reconnection and the error, never a second attempt
  at the machine. One shared list of OPC UA status codes and socket errors
  decides what counts as a dead session at all, so a `BadNodeIdUnknown` is still
  reported rather than retried into the same answer.

  Data-change subscriptions are re-created on the new session, so the IDs an
  agent holds keep working and the changes already buffered survive the outage.
  Neither server now dies at startup when the endpoint is unreachable: it starts,
  says so, and connects on the first tool call that needs a session. The Python
  server also re-probes the optional capabilities on every `tools/list`, as the
  Node server already did, so a server that was down at startup no longer has its
  history and aggregate tools hidden for the rest of the session.
- **Health and diagnostics tool** (#13). `get_server_status` reports, in one
  call, whether the MCP server is connected, to which endpoint and under what
  security, the OPC UA server's own `ServerStatus` (state, current time, start
  time, build info) and its NamespaceArray as `index -> uri`. Both runtimes read
  the standard nodes named in the shared contract (`ns=0;i=2256`, `ns=0;i=2255`)
  and return one record of the new `serverStatus` result shape, so the two
  answers are identical field for field.

  It is the one tool that never fails for being disconnected — it reports
  `connected: false` and the reason instead, which is exactly what makes it
  useful when something else has just failed. Every other tool's "not connected"
  error names it. Calling it also re-establishes a dropped connection, so it
  doubles as "try again now".
- **MCP Registry metadata.** A root `server.json` describes the npm
  distribution, its stdio transport and every environment variable it reads, and
  `packages/server-node/package.json` now carries the matching
  `mcpName: io.github.midhunxavier/opcua`. Unit tests hold the two names, the npm
  identifier and all the version fields together, so the pair that proves package
  ownership cannot drift. Nothing is submitted to the registry by this change —
  the first submission needs a release whose npm tarball carries `mcpName`, which
  the already-published 0.3.0 cannot; [docs/mcp-registry.md](docs/mcp-registry.md)
  has the steps.
- **[ROADMAP.md](ROADMAP.md)** — what exists, what is next, and what is only an
  idea, linked to the issues that track each item.
- **[docs/compatibility.md](docs/compatibility.md)** — which OPC UA servers,
  capabilities, runtimes and clients are actually covered by the test suite,
  separated from what is merely expected to work. No third-party server has a
  recorded result yet; a compatibility issue template collects them.

- **Production tool policy and typed control boundary.** Both runtimes now
  default to an observe-only profile, share tool risk/annotation metadata, and
  enforce profile, tool and exact node/method allowlists on every invocation.
  Control tools require OPC UA channel security unless a lab-only override is
  explicit. A versioned JSON policy, environment overrides, Claude Desktop
  bundle fields, structured audit decisions and cross-runtime E2E tests are
  included.
- **Bounded address-space discovery and typed writes.** `get_all_variables` now
  has root, depth and inspected-node budgets plus cycle detection and a clear
  truncation notice. Writes convert through the target node's OPC UA Variant
  metadata with strict booleans, integer range checks, lossless 64-bit values,
  base64 ByteStrings, ISO DateTimes and arrays. Python batch reads and writes
  now use one OPC UA service call instead of one round trip per node.
- Tools with a declared result shape now advertise MCP `outputSchema` and return
  canonical `structuredContent` from both runtimes while retaining text blocks
  for older clients. The Node connection manager also coalesces concurrent
  connection attempts and cleans up partial sessions deterministically.
- **Real-time data-change subscriptions** (#3). Three tools on both servers —
  `subscribe_opcua_node`, `list_subscriptions`, `unsubscribe_opcua_node` — plus a
  resource, `opcua://subscriptions`. Until now the only way to follow a node was
  to call `read_opcua_node` in a loop; now the OPC UA server pushes each change
  and the MCP server buffers it.

  An MCP tool call is request/response, so a subscription cannot call the agent
  back: the notifications arrive whenever the OPC UA server publishes, long after
  `subscribe_opcua_node` has returned. Each runtime therefore owns the
  subscription and buffers what it delivers, in a ring of `buffer_size` records
  with a `change_count` beside it — so an agent that looks away sees how much it
  missed rather than silently losing it. The records are read back either from
  `list_subscriptions` or, without spending a tool call, from the resource; both
  carry the new `resultShapes.subscriptionRecords` shape, whose `changes` are
  ordinary `historyRecords`.

  An explicit `unsubscribe_opcua_node` reports a delete the OPC UA server
  refuses, rather than answering "success" for a subscription that may still be
  publishing; the caller no longer holds an ID to retry with, so swallowing it
  would hide the leak. Shutdown stays quiet, where a refused delete is the
  normal case rather than news.

  Subscriptions do not outlive the MCP session. Deleting them *before* closing
  the OPC UA session is the part that is easy to get wrong — a session closed
  with subscriptions still attached leaves the OPC UA server publishing into the
  void until their lifetime expires — so Python tears them down in the lifespan's
  `finally` and Node on `SIGINT`, `SIGTERM` and `server.onclose`, the last
  because the usual end of an MCP session is the client closing stdin rather than
  any signal.

  **Not included: `notifications/resources/updated`.** The issue asked for it and
  the two SDK generations no longer agree on what it means — `@modelcontextprotocol/sdk`
  1.x speaks `resources/subscribe` + `notifications/resources/updated`, while the
  Python `mcp` 2.x SDK removed `resources/subscribe` as of protocol 2026-07-28 in
  favour of `subscriptions/listen` streams the Node SDK does not serve, and drops
  `notify_resource_updated` on the floor. Offering it on one runtime only would
  break the interchangeability this repo is built around, so neither does; see
  [docs/architecture.md](docs/architecture.md#why-the-subscriptions-resource-is-polled-not-pushed).
- **The contract-parity test now compares each parameter's declared *type***, not
  only its name and whether it is required. That gap let a real divergence
  through in review: `buffer_size` was annotated `int` in Python and declared
  `number` in the contract, so the Python server advertised `integer` and the
  SDK rejected a `7.9` the Node server truncated to 7. `buffer_size` and the
  pre-existing `num_values` are both counts and are now declared `integer`,
  which is what the Python server has always derived from their annotations. An
  optional `T | None` parameter renders as `anyOf: [{type: T}, {type: null}]`
  rather than a bare `type`, so the check flattens those branches.
- **The contract now defines the resource surface too**, under a `resources` key,
  each entry naming the `resultShape` its document carries. Both servers build
  `resources/list` from it and `tests/e2e/test_contract_parity.py` reads the
  resource from each and checks it against that shape, exactly as it already did
  for tool output.
- **[docs/certificates.md](docs/certificates.md): client certificates and trust
  setup** (#5). Turning encryption on needs a certificate that OPC UA servers
  accept — `subjectAltName` URI, all four key usages, `clientAuth`, RSA 2048 and
  SHA-256 — and then an operator willing to move it from the server's rejected
  list into its trusted one. Both were folklore, or were buried in a testing
  walkthrough that uses throwaway certificates. The new page has an `openssl`
  recipe, the naming and permission rules each runtime imposes, the trust dance
  step by step, and a table mapping the certificate status codes back to what to
  change.
- **Alarms & Conditions: four new tools, on both servers** (#4). Industrial
  systems report abnormal states through the A&C model rather than as plain
  variables, and none of it was reachable before.

  * `subscribe_events` — start collecting events from a notifier node (the
    Server object by default), with an optional severity floor and buffer size.
  * `read_events` — hand over what has arrived since the last read, oldest
    first, and remove it from the buffer.
  * `list_active_alarms` — the conditions the server is retaining right now.
  * `acknowledge_alarm` — acknowledge one, with a comment.

  Events are collected rather than pushed, for the reason #3's data-change
  subscriptions are, and they come down on the same teardown path: an event
  subscription costs the OPC UA server the same as any other until its lifetime
  expires, so both runtimes delete theirs before closing the session.
  `subscribe_events` starts a real subscription whose monitored item parks what
  arrives, and `read_events` drains it. `list_active_alarms` needs no subscription of yours — it makes its own,
  calls ConditionRefresh, and collects the conditions the server replays between
  the RefreshStart and RefreshEnd events.

  `acknowledge_alarm` takes only the `event_id` that was just reported. OPC UA
  needs the condition's NodeId as well, but only one of the two is worth asking a
  model to carry around, so both servers remember which condition each event they
  reported came from. `condition_id` can still be passed for an event from
  elsewhere.

  Two things it will not do quietly. A ConditionRefresh that does not finish
  within `timeout_seconds` is an error rather than a short list — a partial
  answer cannot be told apart from "no alarms", and inventing that one is the
  failure this tool must not have. And when the event buffer overflows between
  reads, `read_events` says how many it lost in the response itself rather than
  only on stderr, which an MCP client never shows: an agent that cannot tell a
  complete event stream from a truncated one reads a burst of alarms as quiet.

  The tools are **not** capability-gated, unlike history and aggregates: every
  OPC UA server has a Server object with an EventNotifier, and one that raises
  nothing simply buffers nothing. A server without A&C is told apart at call time
  instead — `list_active_alarms` reports that its ConditionRefresh failed and
  that the server may not implement A&C, rather than returning an empty list a
  model would read as "no alarms".

  Both servers build one EventFilter from one list of browse paths in
  `contract/tools.json` -> `events`, which is also the field order of the new
  `resultShapes.eventRecords`, so neither can select a field the other reports or
  name it differently. Two details of that list are load-bearing: every path is
  resolved against BaseEventType, which Part 4 §7.4.4.5 says makes a server
  evaluate it without regard to the event's own type (so one filter can select
  `AckedState/Id` from a condition and get `null`, not an error, from a plain
  event); and ConditionId is not a component of ConditionType at all but the
  NodeId attribute of the condition instance, which is what the Acknowledge
  method is called on.
- **The bundled mock raises events.** It announces every change of its alarm
  state — severity 700 for `Alarm active: <reason>`, 100 for `Alarm cleared` —
  so `subscribe_events` and `read_events` have something real to collect. Trigger
  one by writing `true` to `EmergencyStopCommand` (`ns=2;i=25`) and clear it with
  `ResetSystemCommand` (`ns=2;i=26`).
- **A third mock server, `packages/mock-server-alarms`** (node-opcua), with a
  real `ExclusiveLimitAlarm` on a writable `Temperature`. python-opcua's server
  has no condition model at all, so the main mock cannot answer a
  ConditionRefresh or offer an Acknowledge method to call — which makes it the
  right server to prove the *absence* case reads clearly, and the wrong one to
  prove the tools work. This one is a genuine Part 9 implementation, so
  `list_active_alarms` and `acknowledge_alarm` are tested against a real
  condition instance rather than against our own idea of one.

### Changed

- **The tool policy authorises from the contract instead of a list of tool
  names.** Each `control` and `alarm-action` tool now declares a `guard` in
  `contract/tools.json` saying where its sensitive identifiers live —
  `nodeIdPaths`, `methodPaths`, or a `flag` — and the policy walks that
  declaration. **A control tool that declares no guard is denied**, and an
  `accessClass` the contract spelled wrong is denied too, including under the
  `full` profile.

  This closes a fail-open. Argument validation was an if/else chain keyed on
  three hardcoded tool names, and visibility ended in
  `return writableNodes.size > 0` — so a *new* control tool added to the
  contract became visible as soon as one node was writable and was then called
  with **no argument validation at all**. In a repository whose thesis is
  "derive from the contract, never hand-maintain a tool list", it was the one
  place that hand-maintained one. A contract test now also fails if a control
  tool is added without a guard, so the gap is reported when the contract is
  edited rather than discovered later.

  `monitor` tools remain outside the secure-channel gate, now with the reasoning
  written down: that gate exists to stop *control* over a channel anyone can
  read or forge, and a subscription changes nothing in the plant.
- **Node-ID allowlists are canonicalised, and can be pinned by namespace URI.**
  `i=2253` and `ns=0;i=2253` are the same node; matching was raw set membership
  on untrimmed strings, so an entry written one way silently never matched a
  request written the other. Both runtimes now share one canonicaliser — the
  Python one existed in `records.py` and the policy layer did not use it, so
  records agreed on a spelling while the allowlist did not — pinned from both
  sides by `tests/fixtures/node-id-forms.json`.

  More importantly, `OPCUA_ALLOWED_WRITE_NODES` and `OPCUA_ALLOWED_METHODS` now
  accept `nsu=<namespace-uri>;i=5`. A namespace *index* is that node's position
  in the server's NamespaceArray for the current session, so a firmware update
  or a reordered namespace load can move it — and an allowlist written
  `ns=2;i=5` then authorises writes to a **different physical node** with nothing
  reporting anything wrong. Both servers read the NamespaceArray on every
  connect and resolve URI-pinned entries against it. An entry naming a URI the
  server does not publish matches nothing and is reported on stderr.
- **The startup summary distinguishes a secured deployment from a lab override.**
  `describePolicy` printed `insecure-control=enabled` for both, which made the
  override the opposite of conspicuous. It now prints `control=secured`,
  `control=INSECURE-OVERRIDE` or `control=blocked`.

- The README badge and the contribution guide said **Node 18+**; the package has
  required Node 22.13+ since 0.3.0. Both now say so.
- **The Python server now targets the `mcp` 2.x API.** 0.3.0 pinned `mcp[cli]<2`
  because 2.x renamed `FastMCP` to `MCPServer` and the server died on import
  without it; the pin is now `>=2.2.0,<3` and the server imports
  `mcp.server.mcpserver`. The protocol version no longer has to be poked onto the
  private low-level server — `MCPServer` takes `version=` in its constructor.

  Two consequences worth knowing if you depend on this package:

  * **Tool failures must be raised as `ToolError` to stay readable.** 2.x forwards
    a `ToolError`'s message to the client and replaces every other exception's
    with `Error executing tool <name>`, on the grounds that an unanticipated crash
    should not leak its internals. The history and aggregate tools now raise
    `ToolError`, so `Invalid aggregate function. Supported: …` and
    `Failed to read node …: …` still reach the caller, worded as the Node server
    words them. Python `read_history_opcua_node` had no such wrapper at all
    before, so a bad node ID or timestamp surfaced without the `Failed to read
    node …` prefix the Node server adds; the two now agree. (Each SDK still adds
    its own outer prefix, which neither server controls.)
  * **The client models are snake_case.** `result.isError` is `result.is_error`,
    `tool.inputSchema` is `tool.input_schema`, `initialize().serverInfo` is
    `.server_info`. This is a Python-attribute rename only: the wire format, and
    so `contract/tools.json` and the Node server, are untouched.
- **The Node server needs Node 22.13 or newer** (`engines.node` was `>=18`).
  node-opcua 2.183 declares the same floor, and the releases just before it had
  already stopped working on Node 18 in fact if not in writing: 2.182 pulls in
  `hexy` 0.4, which is ESM-only, and `node-opcua-debug` `require()`s it, so the
  server died on import with `ERR_REQUIRE_ESM`. Node 18 went end-of-life in
  April 2025 and Node 20 in April 2026. CI now covers Node 22 and 24, the
  release and publish workflows build on Node 22 — the single-file executable
  embeds the Node that builds it, so that one has to satisfy the floor too — and
  the `.mcpb` manifest asks for the same version.
- **The Node server depends on `node-opcua-client` rather than the umbrella
  `node-opcua` package.** It is an OPC UA client and uses nothing from the server
  half, which the umbrella package's entry point pulled in regardless. That was
  not merely dead weight: `node-opcua-server` and the address-space test helpers
  both read a file relative to their own `__dirname` at *import* time to find
  their `package.json`, which does not exist once bundled, so under 2.183 the
  `.mcpb` failed on connect with `ENOENT … extension/package.json`. Importing the
  client package removes both reads, lets the compiler enforce that this server
  only reaches for client APIs, and takes the `.mcpb` from about 7 MB to under
  one.
- **Node tool failures now return MCP error results** (#61). The shared
  `callTool` error handler sets `isError: true`, so clients can reliably detect
  failed tool calls instead of having to inspect the returned error text.
- **Python tool failures now return MCP error results** (#63). `write_opcua_node`,
  `browse_opcua_node_children` and `call_opcua_method` caught their exception and
  *returned* the message as ordinary text, which the SDK hands back as a
  **successful** tool result — so a client keying on `is_error` saw a failed write
  succeed, and had to read the prose to find out otherwise. They now raise
  `ToolError`, worded as the Node server words it. #61 fixed the mirror image of
  this on the Node side; the two runtimes now agree.

  `browse_opcua_node_children` was the worst of the three, and not only for the
  flag: python-opcua's `Node.get_children()` reads `BrowseResult.References` and
  never looks at the sibling `BrowseResult.StatusCode`, so browsing a node the
  server does not have returned an *empty child list*. `ns=2;i=999999` answered
  `Children of ns=2;i=999999: []` — "this node has no children", for a node that
  does not exist. The server now checks the status itself.

  **Deliberately unchanged: a per-node rejection in a batch.**
  `read_multiple_opcua_nodes` and `write_multiple_opcua_nodes` report per-node
  status inside a successful result, and a node the server rejects is one
  `Error: …` status among them rather than a failed call — promoting it would
  discard the statuses of every other node in the batch. Only a failure of the
  whole operation is an error, which is what `write_multiple_opcua_nodes` now
  raises rather than returning as text.

### Fixed
- **The Node server now follows browse continuation points** (#75). A server may
  cap how many references one `BrowseResponse` carries whatever the client asks
  for, and answer the rest behind a continuation point. The Node server took the
  first result and stopped, so `browse_opcua_node_children` and
  `get_all_variables` returned a *truncated child list as a success* on any node
  wide enough to be paged — a wrong answer delivered confidently, and exactly the
  shape of address space the target hardware has. The Python server has drained
  the points since #2; both now share one helper per runtime, so the traversal
  and the single browse cannot drift apart again. Every result is status-checked,
  the continued ones included: an expired continuation point is now an error
  rather than a short list.

  There is no end-to-end test of this and there cannot be one — `python-opcua`'s
  *server* implements continuation points nowhere and ignores
  `RequestedMaxReferencesPerNode`, so no mock in this repo can emit one. That is
  also why the gap survived this long. It is pinned instead by mirrored unit
  tests on both runtimes (`test/unit.test.mjs`, `tests/unit/test_browse.py`) that
  stub the session.
- **A Python batch read that fails wholesale is now an error result** (#76).
  `read_multiple_opcua_nodes` returned `"Error reading multiple nodes: …"` as a
  *successful* result, so a model saw the failure as data and would reason over
  the excuse as if it were readings. The last survivor of the sweep in #63, which
  fixed four sibling handlers and missed this one because nothing asserted it;
  the Node server has thrown here all along. A per-node rejection is unchanged —
  it stays a status inside a successful result, because promoting it would
  discard every other node's value.
- **The mock OPC UA server now answers a write to a node it does not have**
  (#64). python-opcua bit-tests the AccessLevel of every node a non-admin
  session writes to — every client here, the endpoints being anonymous — and
  reads it off the `DataValue` returned for the node id. For an id the address
  space does not have, that is an empty `DataValue` with a null Variant, so the
  check raised `TypeError` out of the request handler: the mock answered the
  `WriteRequest` not at all and dropped the connection. A batched write then cost
  the client its whole batch after a 15s transaction timeout, including the nodes
  the mock had already written — with no way to tell whether they had moved. The
  mock now screens unknown node ids out of a `WriteRequest` and answers them
  `BadNodeIdUnknown` beside the `Good` of the nodes it wrote, as a conformant
  server does. Test fixture only — no change to either shipped server, which both
  read a node's type before writing it and so never sent the offending request;
  that is also why it takes a bare OPC UA client, in the new
  `tests/e2e/test_mock_server_e2e.py`, to hold the mock to it.
- **The Python server now announces the client certificate's own ApplicationUri**
  (#5). With a certificate configured but no `OPCUA_APPLICATION_URI`,
  python-opcua announced its library default, `urn:freeopcua:client`, while
  node-opcua reads the URI out of the certificate — so the same certificate and
  the same variables reached a server as two different identities depending on
  which runtime was started, and equipment that checks the ApplicationUri against
  the `subjectAltName` (as the spec has it) refused the Python one with
  `BadCertificateUriInvalid`. It now takes the URI from the certificate too, and
  warns when an explicit `OPCUA_APPLICATION_URI` contradicts one. That also makes
  a secured connection expressible from the `.mcpb` bundle, whose fields cover
  the certificate but not the URI. The secured mock grew the check real servers
  make (`--check-client-uri`), so the end-to-end tests can tell a derived
  ApplicationUri from a default that happens to connect.
- **A NodeId in namespace 0 is now spelled the same by both servers.** python-opcua
  omits a zero namespace from a NodeId's text form (`i=2253`) where node-opcua
  writes it out (`ns=0;i=2253`); the Python server passed that difference
  straight through. It surfaced with the event tools, where an `event_type` is
  almost always in namespace 0 and `source_node` often is, but it was always
  reachable through a history value of type NodeId. Both now emit the namespace
  explicitly, and `tests/fixtures/value-encoding.json` pins the case.
- **The e2e suite no longer borrows another checkout's mock OPC UA server** (#46).
  Each mock fixture picked a fixed port (4840/4841/4843) and, finding something
  already listening there, adopted it. With one developer on one checkout that was
  a convenience; with a worktree per task it meant two sessions sharing a mock as
  it started, warmed up and was torn down, and failures that moved between tests
  from run to run. The aggregate tests suffered most, because adopting a running
  mock also skipped the 20s warmup their arithmetic over the ramp depends on.
  Every mock is now started by its fixture on a free ephemeral port, so the warmup
  always applies to the history the tests then read. `opcua-mock-server` takes
  `--endpoint` for this (default unchanged); the aggregate mock already had
  `AGGREGATE_MOCK_PORT`. Setting `OPCUA_SERVER_URL` or
  `OPCUA_AGGREGATE_SERVER_URL` still points the suite at a server you manage
  yourself, and is now the only way it will use one. Test harness only — no
  change to either shipped server.

## [0.3.0] — 2026-09-11

### Added
- **OPC UA connection security is configurable** on both runtimes, through the
  same environment variables: `OPCUA_SECURITY_POLICY`, `OPCUA_SECURITY_MODE`,
  `OPCUA_CLIENT_CERT`, `OPCUA_CLIENT_KEY`, `OPCUA_APPLICATION_URI`,
  `OPCUA_USERNAME` and `OPCUA_PASSWORD`. Until now both servers hardcoded `SecurityPolicy.None` /
  `MessageSecurityMode.None` and an anonymous session, so there was no way to
  reach a server that requires encryption or a login — the documented "not for
  production" caveat was a limitation of the code, not a choice.

  Policies: `None`, `Basic128Rsa15`, `Basic256`, `Basic256Sha256`, plus
  `Aes128_Sha256_RsaOaep` and `Aes256_Sha256_RsaPss` on the Node runtime
  (`python-opcua` does not implement the AES suites, and says so by name rather
  than reporting an unknown policy). Names are case-insensitive; a policy on its
  own implies `SignAndEncrypt` rather than silently signing only.

  The configuration is validated at startup and a combination OPC UA cannot
  honour — a mode without a policy, a policy without a client certificate, a
  certificate path that does not exist, half a credential — exits with
  `Configuration error: …` naming the variable, identically on both runtimes,
  instead of failing later against live equipment. The Python capability probes
  now connect with the same security as the session they precede.

  **The default is unchanged**: with no variables set, both servers still
  connect unencrypted and anonymous, and now log a warning to stderr saying so.
  That warning keys on the policy alone — credentials authenticate a session but
  encrypt nothing, and a password on a `None` channel is sent in clear text
  unless the server's user-token policy protects it, which earns a second
  warning of its own.

- A **secured mock OPC UA server** in the test suite
  (`tests/fixtures/secure_opcua_server.py`, port 4843), offering only
  Basic256Sha256 endpoints and requiring a username. The end-to-end suite now
  drives both runtimes through an encrypted, authenticated session — read, write,
  `Sign` and `SignAndEncrypt` — and asserts the failure modes too: a wrong
  password yields `BadUserAccessDenied` rather than a session, an unsecured
  client finds no endpoint to fall back to, and the password never reaches the
  logs. Certificates are generated per test session, not committed. What no mock
  can cover is a real server's certificate trust list, so enabling security
  against real equipment still needs a manual first connection.
- **Download-and-use distribution.** Getting started previously meant having Node
  or Python on `PATH`, then finding and hand-editing `claude_desktop_config.json`
  — three walls in front of an audience of automation engineers, often on
  locked-down machines on air-gapped plant networks. Three new routes in, none of
  which needs a runtime or a text editor:

  - **An `.mcpb` MCP bundle** (~1.2 MB) for Claude Desktop: one file, dragged
    into Settings → Extensions. It carries the server and its whole dependency
    tree bundled into a single JavaScript file, Claude Desktop supplies the Node
    runtime, and the OPC UA endpoint is rendered as a settings field from the
    manifest's `user_config`. Built by `npm run build:mcpb`.
  - **Single-file executables** for Linux, macOS and Windows, from both runtimes
    (`npm run build:sea` via Node's single-executable support, and PyInstaller
    for Python). No Node, no Python, no network access at startup. Neither can be
    cross-compiled, so `.github/workflows/release.yml` builds one per OS and
    attaches them to the GitHub release.
  - **`opcua-mcp-server --install claude-desktop`**, in both runtimes, which
    writes the client config itself: correct path per OS, merged into whatever is
    already there, previous file backed up, written atomically, and refusing
    rather than overwriting an existing `opcua` entry without `--force`. It
    records *absolute* paths to the interpreter and the server, because desktop
    apps are launched from the GUI and do not inherit a login shell's `PATH` —
    the most common reason an MCP server that works in a terminal fails to start
    in Claude Desktop. Also `--url`, `--dry-run`, `--force`, `--version`,
    `--help`.

  See [docs/install.md](docs/install.md). Every one of these artifacts is built
  and driven against a live OPC UA server in `tests/smoke/`.

- `python -m opcua_mcp_server` as an equivalent of the console script.

### Changed
- **Importing `opcua_mcp_server` no longer connects to an OPC UA server.** The
  capability probe ran at package-import time, so `import opcua_mcp_server` — or
  `--help` — would sit through a connection timeout. `main` and `mcp` are now
  resolved lazily (PEP 562) and the console script entry point moved to
  `opcua_mcp_server.cli:main`, which starts the server only when it is going to
  serve. `from opcua_mcp_server import main, mcp` still works.

- **BREAKING (Node server): `read_history_opcua_node` and
  `read_aggregate_opcua_node` now return flat records instead of raw
  `DataValue` JSON.** The two servers answered the same tool call with different
  shapes — the Node server with node-opcua's internal representation
  (`{"value": {"dataType": "Double", "value": 51.75}, "statusCode": {"value": 1},
  "sourceTimestamp": …}`), the Python server with `{value, timestamp, status}`.
  Both were "correct": `contract/tools.json` unified tool names, descriptions and
  capability gating, but said nothing about output. A client — or a model — that
  learned one server's output misread the other's.

  The contract now declares the shape, under `resultShapes.historyRecords`, and
  both servers produce it:

  ```json
  { "value": 51.75, "timestamp": "2026-09-09T13:36:01.139Z", "status": "Good" }
  ```

  One record per historical value or aggregate interval, one MCP content block
  per record. Anything consuming the Node server's `sourceTimestamp` /
  `statusCode.value` / nested `value.value` must move to `timestamp` / `status` /
  `value`. (#23)

- **BREAKING (Python server): history timestamps are ISO-8601 UTC**, e.g.
  `2026-09-09T13:36:01.468091Z` rather than `str(datetime)`'s
  `2026-09-09 13:36:01.468000` — space-separated and with no zone. The same tools
  already *accept* ISO-8601 for `start_time`/`end_time`, so their output now
  round-trips back into their input. (#23)

- **BREAKING (both servers): every OPC UA value type now has one canonical JSON
  encoding.** A Double arrives as `51.75`, not `"51.75"`, and an aggregate
  interval the server holds no data for is `null` rather than the string
  `"None"`. Beyond the primitives, the two client libraries represent the same
  reading with entirely different native types, so encoding keys on the OPC UA
  data type rather than the language one — without that, a ByteString was
  `[97, 98, 99]` from Node and `"b'abc'"` from Python, an Int64 of `-5` was
  `[4294967295, 4294967291]` from Node and `-5` from Python, and NodeId,
  StatusCode, DateTime and LocalizedText each had two language-specific
  spellings.

  ByteString is base64, DateTime is ISO-8601 UTC, Guid is a lower-case UUID,
  NodeId / StatusCode / QualifiedName / LocalizedText are their canonical text
  forms, and a 64-bit integer too large for a JSON number (or a non-finite
  Double) becomes a string rather than being silently rounded. Structured and
  opaque types (ExtensionObject, XmlElement) still degrade to a string form that
  may differ between runtimes.

  `tests/fixtures/value-encoding.json` holds the table; both unit suites build
  the native value for every case and assert the same JSON comes out, so a case
  cannot be added without both runtimes handling it. (#23)

### Fixed
- Corrected the READMEs for the published packages: the PyPI long description
  claimed Python 3.13+ (the floor is 3.10), had no install instructions for the
  published package, and linked with `../../` relative paths that are dead links
  when rendered on PyPI — as does the npm one. The root README carried a
  hardcoded personal path in a config example and invented tool output that
  matched nothing the server produces.

  **These reach npmjs.com and pypi.org only on the next release**, because each
  package's README ships inside its artifact and registry pages are frozen per
  version.

## [0.2.1] — 2026-09-09

Completes the 0.2.0 release. **0.2.0 reached npm only** — the PyPI job failed to
build, so this is the first version published to both registries.

### Fixed
- **The Python sdist could not build a wheel.** The shared tool contract was
  force-included from `../../contract/tools.json`, a path that exists in a
  checkout but can never exist inside an sdist. `uv build` (and `pip install`
  from an sdist) builds the wheel *from the sdist*, so it failed with
  `FileNotFoundError: Forced include not found`. The sdist now carries its own
  copy of the contract and a build hook injects it into the wheel from whichever
  location is present.

  The artifact smoke tests missed this because they built with `uv build --wheel`,
  straight from the source tree, never exercising the sdist path. They now build
  both and additionally unpack the sdist outside the repo and build a wheel from
  it alone.
- Both publish jobs are now idempotent (`skip-existing` on PyPI, a version check
  on npm), so a partial release like 0.2.0's is safe to re-run.


## [0.2.0] — 2026-09-09

First release published as **`opcua-mcp-server`**. The previously published
`opcua-mcp-npx-server` is deprecated in favour of this name.

### Changed
- Renamed the npm package `opcua-mcp-npx-server` → `opcua-mcp-server` (the old
  name will be deprecated on npm with a pointer to the new one).
- The Python server now identifies itself over MCP as `opcua-mcp-server` instead
  of `OPCUA-Control`, matching the Node server — both runtimes are the same
  product and now say so. Purely informational in the MCP handshake; it does not
  affect the server key in your client config.
- The Python server now reports a real version in the MCP handshake (it
  previously reported none).
- Both servers now single-source their version: Node from `package.json` (staged
  into `build/version.json` at build time), Python from its installed
  distribution metadata. The version literal in `src/index.ts` is gone, and
  `tests/test_version_parity.py` fails the build if the manifests drift apart or
  a hardcoded version is reintroduced.
- Documentation and code now call the second implementation the **Node** server
  rather than the "npx" server; `npx` refers only to the command. The pytest
  selector is now `-k "[node]"` / `-k "[python]"` — plain `-k node` would also
  match test names like `test_read_opcua_node`.
- Restructured the repository into a `packages/` monorepo layout with a single
  uv workspace.

### Added
- `docs/architecture.md` — how the two runtimes, the shared contract and the
  capability gating fit together, plus the three invariants that are easy to
  break (stdout is the transport, nothing hardcodes a version, the published
  artifact is what users get).
- `read_aggregate_opcua_node` is now implemented on the **Python** server too,
  with the same capability gating and the same error wording as the Node server.
  It was previously Node-only, which made the README's "two interchangeable
  implementations" claim untrue.
- A second, aggregate-capable mock OPC UA server (`packages/mock-server-aggregate`,
  port 4841) and `tests/e2e/test_aggregate_e2e.py`, which check aggregate output
  arithmetically against the mock's known ramp rate rather than merely for
  non-emptiness. The main mock keeps advertising no aggregate functions on
  purpose, so the suite can still assert the tool is hidden when unsupported.
- **Python 3.10+ is now supported** (was 3.13+). Nothing in the codebase needed
  3.11 or newer; the floor simply excluded most installed Pythons, including the
  3.9–3.11 common in industrial environments. Verified by installing and driving
  the server on 3.10, not by inspection.
- CI now runs the end-to-end suite across the versions the manifests actually
  claim — Python 3.10/3.13 and Node 18/20/22 — instead of only Python 3.13 and
  Node 20.
- The Node server is split from one 857-line `index.ts` into `config`,
  `contract`, `dates`, `connection` (client/session lifecycle plus the capability
  probes), `tools` (the tool implementations and dispatch) and `index` (MCP
  wiring and the entry point). Adding a tool now touches `tools.ts` and the
  contract, nothing else.
- The Python server is now a real package (`src/opcua_mcp_server/`) split into
  `config`, `contract`, `datetimes`, `capabilities` and `server`, instead of a
  single 505-line flat module. The wheel now installs exactly one top-level name;
  it previously dropped two files (`opcua_mcp_server.py` and
  `opcua_mcp_server_contract.json`) directly into `site-packages`, which is why
  the bundled contract needed a namespaced filename to avoid colliding with other
  distributions. The contract now ships inside the package.
- Unit-test tier (`tests/unit/` and `packages/server-node/test/`) covering the
  pure logic — ISO-8601 parsing, contract invariants, version manifests — with no
  OPC UA server and no MCP transport. 42 Python unit tests run in ~0.2s against
  ~50s for the end-to-end suite. The Node tests use the built-in `node:test`
  runner, so the package gains no dependency.
- The Node server module is now importable without starting a server: the entry
  point is guarded, and `toDate`/`OPCUAMCPServer` are exported for testing. The
  guard resolves symlinks, because `npx` invokes the `node_modules/.bin` shim and
  a naive `import.meta.url === process.argv[1]` check would never match.
- Artifact smoke tests (`tests/smoke/`): build the npm tarball and the Python
  wheel, install each into an isolated location, and drive the installed entry
  point over MCP from a working directory outside the repo. Run as their own CI
  job; deselected from the default suite with `-m "not smoke"`.
- Lint, format and typecheck gates: ruff for Python, Prettier + `tsc --noEmit`
  for TypeScript, wired into a fast `lint` CI job that runs alongside the
  end-to-end suite. Plus `.editorconfig`, Dependabot, `CODEOWNERS` and an issue
  template chooser.
- `read_history_opcua_node` tool — read historical (timestamped) values for a node.
- `read_aggregate_opcua_node` tool — server-side aggregate reads, exposed only
  when the server advertises aggregate function support (capability gating).
- End-to-end test suite (`tests/`) driving both the Python and Node servers over
  stdio against the mock OPC UA server.
- `CONTRIBUTING.md`, `TESTING.md`, and `EXAMPLES.md` documentation.
- `LICENSE`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and CI workflow.

### Fixed
- **The Node server now falls back from IPv6 to IPv4 when connecting.** Node 20+
  enables Happy Eyeballs by default; Node 18 does not, so the documented default
  endpoint `opc.tcp://localhost:4840` resolved to `::1` and failed outright
  against an OPC UA server listening on IPv4, rather than retrying `127.0.0.1`.
  Found by the new Node 18 CI job. The server now opts in explicitly.
- **The Node server no longer silently returns data for the wrong day.**
  `toDate` relied on V8's `Date` parser, which rolls an out-of-range day over
  into the next month, so a history read for `2026-02-30` quietly returned
  `2026-03-02` data instead of failing. It now validates the calendar date
  arithmetically — which also restores parity with the Python server, whose
  `datetime.fromisoformat` always rejected these.
- **The Python server no longer breaks on a fresh install.** Its `mcp[cli]>=1.9.1`
  dependency had no upper bound, so a clean `pip`/`uvx` install resolved mcp 2.x,
  where `FastMCP` was renamed to `MCPServer` — the server then died on import with
  `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. Pinned to `<2`.
  The committed `uv.lock` pinned 1.x, so every existing test and CI run passed
  while installs from the published package would have failed; the new artifact
  smoke tests are what surfaced it.
- Boolean and value handling across the server and clients.
- OPC UA method calls.
- Python `read_history_opcua_node` now takes `start_time`/`end_time` as ISO-8601
  strings and rejects malformed input with the same message as the Node server
  (`Invalid date/time: … Use ISO 8601, e.g. 2026-04-23T17:40:00Z`).
- The shared tool contract is now bundled inside the Python wheel, so a
  pip/uvx-installed `opcua-mcp-server` no longer fails on import with
  `FileNotFoundError` when run outside the repo layout.

## [0.1.2] — published as `opcua-mcp-npx-server`

Initial published versions on npm, under the old name `opcua-mcp-npx-server`,
with the seven core OPC UA tools (read, write, browse, read/write multiple, call
method, get all variables). This is the only name published to date; the rename
to `opcua-mcp-server` ships with the next release.

[Unreleased]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/IndustriAgents/OPCUA-MCP/releases/tag/v0.1.2
