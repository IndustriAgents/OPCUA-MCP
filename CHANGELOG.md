# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Changed
- **Node tool failures now return MCP error results** (#61). The shared
  `callTool` error handler sets `isError: true`, so clients can reliably detect
  failed tool calls instead of having to inspect the returned error text.

### Added
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

### Fixed
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

[Unreleased]: https://github.com/midhunxavier/OPCUA-MCP/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/midhunxavier/OPCUA-MCP/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/midhunxavier/OPCUA-MCP/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/midhunxavier/OPCUA-MCP/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/midhunxavier/OPCUA-MCP/releases/tag/v0.1.2
