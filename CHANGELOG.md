# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed — **breaking** for `operator` / `full` without `OPCUA_SERVER_CERT`
- **Control now needs an authenticated OPC UA server, not only an encrypted
  channel** (#134). The control gate checked `OPCUA_SECURITY_POLICY != None`,
  which says the traffic is encrypted and nothing about who it is encrypted
  *to*: both client libraries take whatever certificate the endpoint presents,
  so anyone able to answer for the endpoint (DNS, ARP, a compromised switch)
  got an encrypted channel and, with `operator` or `full`, the writes, method
  calls and alarm actions that came with it. Control tools are now offered only
  when the server's certificate is pinned with `OPCUA_SERVER_CERT` — the one
  server-identity check both runtimes have — and are otherwise hidden and
  refused. Reads, browsing and monitoring over an encrypted-but-unverified
  channel are unchanged.

  **Upgrading:** a deployment on `operator` or `full` with a security policy but
  no `OPCUA_SERVER_CERT` loses its control tools. Pin the server's certificate
  (see [docs/certificates.md](docs/certificates.md#the-other-direction)); or,
  for a lab only, set the new `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL=true`
  (policy file: `allow_unverified_server_control`). Deployments on
  `SecurityPolicy=None` with `OPCUA_ALLOW_INSECURE_CONTROL=true` are unaffected;
  that override still means exactly "control over an unsecured channel", and
  deliberately does **not** also accept an unverified server on a secured one —
  encryption and peer authentication are independent properties, and consent to
  lacking one is not consent to lacking the other.
- **A pinned certificate outside its validity window refuses to connect.**
  Neither client library checks the dates of a pinned certificate, so an expired
  pin went on "verifying" the server indefinitely. Both runtimes now check at
  every connect and fail closed, naming the file and the date.
- **Every request is bounded before it reaches the OPC UA server (#139).** New
  limits in `contract/tools.json` -> `limits`, enforced identically by both
  runtimes and refused with a message naming the limit and the value:
  `maxNodesPerWrite` 100, `maxMethodArguments` 64 (both also the input schemas'
  new `maxItems`, beside `maxNodesPerRead` 500), `maxRequestBytes` 1 MiB of
  arguments, `maxStringBytes` 128 KiB per string, `maxByteStringBytes` 64 KiB
  per decoded ByteString, `maxArrayItems` 10000 per array, `maxNestingDepth` 8,
  and `maxEventBufferSize` 10000 (a clamp on `subscribe_events`). An aggregate
  read is now held to `maxHistoryValues` intervals. The request-wide bounds run
  before validation and authorization, so an oversized request never allocates
  an OPC UA object; the validator gained `maxItems`.
- The refusal for a batch read over 500 nodes is now the generic `tooManyItems`
  sentence — "read_opcua_nodes accepts at most 500 entries in node_ids, got 501.
  Nothing was sent to the OPC UA server; split the request." — rather than the
  read-only `tooManyNodes` one, which is gone.
- `write_opcua_nodes`'s description now says a batch is not a transaction.

### Changed — **breaking** for inputs the two runtimes read differently (#157)
- **The two runtimes now agree on what reaches the plant** (#157, group A). On
  some inputs the same tool call could make the Node and Python servers write a
  different value, read a different window of history, or disagree about whether
  a call worked. Each rule below is now one table under `tests/fixtures/` that
  both runtimes' unit suites run: `datetime-parsing.json`, `write-coercion.json`,
  `method-arguments.json`, `status-severity.json`, plus new rows in
  `value-bounds.json`.

  **Breaking: some inputs that used to be accepted are now refused.** Each was
  accepted by only one runtime, or accepted by both with different results:

  | Input | Before | Now |
  |---|---|---|
  | Date/time with no timezone, e.g. `2026-04-23T17:40:00` (history `start_time`/`end_time`, DateTime writes) | Node: the host's local time. Python: UTC | Refused: *"… has no timezone … Add Z for UTC or an offset such as +02:00"* |
  | `04/23/2026`, `April 23, 2026`, `2026`, `2026-04-23` (no time), `T24:00` | Node accepted | Refused |
  | `20260423T174000Z`, `2026-W17-4` | Python 3.11+ accepted | Refused |
  | `2026-02-30` in a DateTime **write** | Node wrote 2 March | Refused (history already refused it) |
  | Dates before 1601 or after 9999 | Varied | Refused: an OPC UA DateTime cannot carry them |
  | Numeric strings `"0x10"`, `""`, `"Infinity"` (writes, method arguments, policy bounds) | Node read 16, 0, Infinity | Refused / not a number |
  | Numeric strings `"1_000"`, `"inf"`, `"nan"`, `"+5"`, `"007"`, `".5"`, non-ASCII digits | Python (and for `+5`/`007`, both) accepted | Refused / not a number |
  | `true` written to a Double/Float/integer node | Python wrote 1.0 | Refused. `true` is not 1 |
  | `[5]`, `["5"]`, `[]` written to a **scalar** node | Node wrote 5, 5, 0 | Refused |
  | A number or boolean written to a String node | `"1"`/`"1.0"`, `"true"`/`"True"` | Refused. Send a string |
  | A LocalizedText/QualifiedName written as an object | Node passed it through, Python wrote `str(dict)` | Refused. Send the text |
  | A Guid with braces, `urn:uuid:` or no hyphens | Python accepted | Refused. The canonical 8-4-4-4-12 form only |
  | ByteString given as a number | Python base64-decoded `str(n)` | Refused |
  | An Int64/UInt64 JSON number beyond ±2^53−1 | Node had already rounded it | Refused. Send it as a decimal string |
  | A Float outside ±3.4e38 | Node wrote Infinity, Python raised mid-encode | Refused |
  | Method with **no** InputArguments, argument `null`, an array or an object | Node sent `"null"`, `"1,2"`, `"[object Object]"`; Python `"None"`, a typed array | Refused before the call is sent |

  **Now accepted where one runtime refused:** the integer `5.0` and the strings
  `"5.0"` and `"1e3"` for an integer node (Python refused), `1`/`0` for a
  Boolean node, and the nil and version-7 Guids (Node refused).

  **Method arguments with a derived DataType** (`Duration` i=290, `UtcTime`,
  an enumeration) are sent as the built-in type they derive from: a Duration as a
  Double, an enumeration as an Int32. Before, Node failed the call and Python
  *guessed every argument and sent the call anyway*. A type with no built-in
  base is refused before anything is sent. With no InputArguments, a JSON number
  is sent as a Double on both runtimes (Python used to send an int as an Int64).

  **Good subcodes are successes on both runtimes**, per OPC UA Part 4 §7.39's
  severity bits, and the subcode is reported in `status` so nothing is hidden. A
  `GoodLocalOverride`/`GoodClamped` read returns its value (Node returned null),
  write type inference and `max_change` use it, browse `include_values` keeps it,
  a `GoodNoData` history or event-history range is an empty list (Node failed
  it), and a method call or alarm action answered with a Good subcode succeeds
  and reports that status. Python used to report `"Good"` for a method call or
  alarm action whatever the server answered.

  Refusal messages that echo a value (`Cannot convert "0x10" to Double`) are now
  identical on both runtimes, as the fixtures check.


### Fixed
- **The Claude Desktop bundle could not configure server-certificate pinning,
  X.509 user login or the audit trail** (#133). Claude Desktop passes a bundled
  server exactly the environment its manifest declares, and the hand-written
  manifest had fallen behind the runtimes: `OPCUA_SERVER_CERT`,
  `OPCUA_APPLICATION_URI`, `OPCUA_USER_CERT`, `OPCUA_USER_KEY`,
  `OPCUA_ALLOW_OUT_OF_RANGE_WRITES`, `OPCUA_AUDIT_FILE` and `OPCUA_OPERATOR_ID`
  were supported by both servers and unreachable from the one-click install.
  `server.json` was missing six of the same. Both now list every variable, and
  the private-key paths join the password in being marked sensitive (MCPB) /
  `isSecret` (registry). The smoke test that claimed to check "every security
  setting" while pinning six by hand now derives its expectation from the schema
  below.
- **A blank `OPCUA_SERVER_URL` now means the default endpoint on the Python
  runtime too.** It was read as an endpoint of `""`, where the Node runtime and
  every other setting treat blank as unset — as an MCP client sends an optional
  field nobody filled in.
- **Every release skipped the Alarms & Conditions tests, and stayed green**
  (#142). `publish.yml` installed the aggregate mock but not the alarms mock, and
  the alarm fixture skips when its mock is missing — so the gate every published
  package passed through never ran an alarm test. The E2E setup is now one
  composite action (`.github/actions/conformance`) shared by CI, `publish.yml`
  and `release.yml`, so a PR and a release prove the same thing.
- **A skip can no longer hide a subsystem from a gate.** With
  `OPCUA_TESTS_REQUIRED=1` — set by CI, publish and release — any skip fails the
  run unless explicitly allowlisted, and every test group (each runtime, security,
  history, aggregates, events, alarms, resilience, parity, each executable) must
  execute at least one passing test. Every run prints a per-group summary.
- **Release executables are driven over MCP on every platform before they are
  attached.** `release.yml` checked `--version` and an install dry run; it now
  runs the binary smoke tests on Linux, macOS and Windows, which add a real read,
  a refused write under the default profile, and a clean exit when the client
  closes stdin. `release.yml` also builds nothing until the full suite has passed
  on the tag.
- **The Node server outlived its MCP client whenever an OPC UA session was
  open.** The SDK's stdio transport listens for data on stdin but not for its
  end, so closing the client never reached the shutdown path, and node-opcua's
  keep-alive timers held the process — and its OPC UA session — open as an
  orphan. It now shuts down (subscriptions, then session, bounded at 5 s) and
  exits when stdin closes. Found by the new binary smoke test; the Python
  runtime already exited.
- `uv.lock` recorded the workspace packages at 0.5.0 after the 0.5.1 release.
- **The Node server never started against an unreachable endpoint with
  `OPCUA_RECONNECT_MAX_RETRY=-1` (#136).** It awaited its first OPC UA
  connection before opening the MCP stdio transport, and handed `-1` to
  node-opcua's `connectionStrategy` as it was — where it means a `connect()`
  that never settles. The MCP client saw a server that never answered
  `initialize`. Python bounded the round but still held `initialize` back for
  all of it: 15s with the default delays and `-1`.

  Both runtimes now open the MCP transport first and run the warm-up in the
  background. `tools/list` and `get_server_status` wait for it for at most 3s
  from its start — so against a reachable plant the first catalogue is still
  the whole one, which is why the warm-up had been moved in front of the
  transport. A tool call that arrives during the warm-up (or any other
  connection attempt) waits for it to end *before* it is authorized and audited,
  not merely before its request goes out: the policy resolves `nsu=` allowlist
  entries through the namespace mapping bound on connect, and the audit record
  names the session the call rides on (#105, #107).
  Past that window `get_server_status` does not join a round someone else
  started: it answers at once with `connected: false` and "Still connecting to
  the OPC UA server at … (last failure: …)" (a new shared `stillConnecting`
  template in `contract/tools.json`). A read during the outage fails with the
  usual "Not connected to the OPC UA server at …" once its round ends.

  `-1` now means the same on both runtimes: no round is ever the last, but each
  is four retries long, so no single request and not the start-up can wait
  forever. node-opcua is handed the round's length; any positive count still
  has it repair a dropped channel with no limit of its own.
- **`OPCUA_RECONNECT_MAX_RETRY` is validated identically on both runtimes.** It
  must be a whole number from `-1` to `1000`. `2.5` used to reach node-opcua as
  2.5 while Python truncated it to 2, `-0.5` passed the `>= -1` floor, `0x10`
  was accepted by Node only, and `1e9` was accepted by both — into a loop that
  built a billion delays. The rules are one shared table,
  `tests/fixtures/reconnect-settings.json`, driven by both test suites.
- **Node refused every connection when the two delays were equal.** node-opcua's
  backoff library requires `OPCUA_RECONNECT_MAX_DELAY_MS` to be strictly greater
  than `OPCUA_RECONNECT_INITIAL_DELAY_MS`, so a setting such as `1000..1000`,
  which Python accepts, failed every connect that allowed a retry at once —
  plant up or not — with
  "The maximal backoff delay must be greater than the initial backoff delay".
- **Shutting down ends a connection round instead of waiting it out.** Node
  disconnects the client that is still dialling, and Python's backoff waits on
  an event its new `close()` sets rather than in `time.sleep`, so a server asked
  to stop no longer dials a plant that is down for the rest of the backoff.
- Node now passes `endpointMustExist` rather than the deprecated
  `endpoint_must_exist`, which logged a warning on every connect.
- **History continuation points are released.** A read the OPC UA server cut
  short returned a continuation point that neither runtime looked at or gave
  back, so the server held it until the session closed — and a server holds only
  so many, after which history reads fail with `BadNoContinuationPoints`. It is
  now noticed (it is what `serverLimit` means) and released at once. On the
  Python runtime this meant reading raw history through `Node.history_read`,
  because python-opcua's `read_raw_history` discards the point.
- **A browse that could not list part of the address space said nothing.** A
  node below the root that refused to be browsed was skipped silently, so its
  subtree was missing from a result that looked complete. `completeness` now
  reports it as `unbrowsable`.
- **Python: an invalid `start_time`/`end_time` on `read_event_history` crashed
  the tool** with the SDK's generic *"Error executing tool"*. It now gets the
  same refusal as on Node (#157, B11).

### Added
- **The identity status is reported everywhere control is decided.**
  `get_server_status` carries a new `server_identity` object
  (`channel_secured`, `server_authenticated`, `authentication_method`, and
  `control`: `secured`, `INSECURE-OVERRIDE`, `UNVERIFIED-OVERRIDE` or
  `blocked`), even while disconnected. Every control audit record carries the
  same `control` value, so a write let through by a lab override says so in the
  trail. The startup line reads e.g.
  `Tool policy: profile=operator control=blocked server-identity=unverified`.
- **Control refusals say what to set.** A control tool refused by the channel
  gate now fails with `controlNeedsSecureChannel` or
  `controlNeedsVerifiedServer` (new in `contract/tools.json` → `errors`), each
  naming the variables that would open it, instead of "disabled by
  OPCUA_PROFILE=full" — which blamed the profile the operator had just set.
- `OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL` in `contract/config.json`, and so in
  the generated MCPB bundle settings and MCP Registry metadata.
- `tests/fixtures/control-gate.json`: all 60 combinations of profile, channel,
  pin and override, driven through both runtimes.
- **`contract/config.json`, one machine-readable definition of the configuration
  surface** (#133). Every `OPCUA_*` variable with its stable key, category, type,
  choices (and per-runtime subsets), minimum, default, secrecy, security
  consequence, supporting runtimes and the surfaces it belongs on. The bundle's
  `user_config` and `mcp_config.env` and `server.json`'s environment variables are
  generated from it by `npm run config:generate` (`packages/server-node/scripts/
  config-artifacts.mjs`); CI runs `npm run config:check`, and
  `tests/unit/test_config_schema.py` fails if either runtime reads a variable the
  schema omits or the reverse, if a README's configuration table misses one, or
  if a parser disagrees with a declared choice, minimum or blank default — the
  same cases drive the Node parsers in `test/config-schema.test.mjs`. The schema
  ships in both packages (`build/config.json`; `opcua_mcp_server/config.json`,
  loaded by `load_config_schema()`), so the installers (#135) and generated
  reference docs (#149) can consume it rather than keep another list.
- **Results say whether they are whole, as a field (#137).** Every tool that can
  return fewer records than its request covered — `read_opcua_history`,
  `read_event_history`, `read_events`, `browse_opcua_nodes` and the three
  subscription tools — now returns a `completeness` object beside `result` in
  `structuredContent`, on every call: `complete`, `reasons` (`requestLimit`,
  `contractLimit`, `serverLimit`, `bufferOverflow`, `unbrowsable`), `returned`,
  `truncated`, `limit`, `dropped`, `remaining` and `continuation`. It was prose
  before — a capped history read and an overflowed event buffer each appended a
  trailing text block that was deliberately kept out of the structured result,
  so a client reading `structuredContent` could not tell a partial answer from a
  complete one. The shape is `contract/tools.json` -> `completeness`, it is part
  of each tool's advertised `outputSchema`, and both runtimes build it from one
  table, `tests/fixtures/completeness.json`. `result` is unchanged, and the text
  notices are still appended for text-only clients.
- **`continuation` says how to get the rest, statelessly.** A forward history
  read that stopped early returns `{"start_time": <last timestamp>}` to merge
  into the same call; `read_events` returns `{}` (call again). Arguments rather
  than a server-held token, so nothing can go stale or outlive a session.
- **Each subscription record reports `dropped`**: the changes its ring buffer
  discarded, which was only derivable before by subtracting `changes` from
  `change_count`.
- **A server's own `OperationLimits` are honoured (#139).** `MaxNodesPerRead`,
  `MaxNodesPerWrite`, `MaxNodesPerBrowse` and
  `MaxNodesPerTranslateBrowsePathsToNodeIds` are read once per session. Every
  read this server sends — `read_opcua_nodes`, a write's read-first type
  inference, browse's value detail, the engineering-unit properties — goes out
  in consecutive chunks no larger than the server's limit, in order, each node
  keeping its own status. A write batch over `MaxNodesPerWrite` is **refused
  rather than split**, before anything is sent: a batch is one Write, and
  splitting it would add the case where one part moved the plant and the next
  never arrived. The bundled mock now publishes 100 and 50, so both paths run in
  the suite.

### Documentation
- **Both runtimes are first-class, and that is now a written promise rather
  than a habit** ([ADR 0001](docs/adr/0001-two-first-class-runtimes.md), #143).
  The Python and Node packages meet the same conformance suite, security
  baseline, artifact checks and release gate, and ship together at one version;
  a divergence between them blocks the release of *both*, not only the runtime
  at fault. The ADR records why this model won over a primary-plus-compatibility
  tier and over retiring one runtime, what exactly is guaranteed to match (names,
  schemas, behaviour, errors, configuration, security, bounds, release timing)
  and what is not (speed, log wording, a client library's own error reason), the
  runtime floors and end-of-life policy, the tests required before either
  package ships, how a divergence is reported, and how the model itself would
  be changed. It also settles the shared strategy for #138, #141 and #144. The
  first ADR, so `docs/adr/` gains an index and a template.
- **What the two runtimes do not share by design is declared, in one
  machine-readable place.** `contract/runtime-differences.json` lists thirteen
  deliberate differences — among them the two Node-only AES security policies,
  the Python runtime's extension-based PEM/DER rule, the MCP protocol generation
  each SDK speaks, the Node-only `.mcpb` bundle and registry listing, how each
  repairs a dropped connection, and node-opcua's on-disk PKI folder — each with
  the rationale that makes it allowed. Accidental divergences are deliberately
  *not* in that file: the ones known today are bugs, tracked in #157 (about
  thirty, found while writing the ADR — the most serious can make the two
  runtimes write a different value or read a different time window for the same
  call) and #136, and `docs/compatibility.md` now lists them with the input
  habits that avoid them until they are fixed.
  `tests/unit/test_runtime_differences.py` checks the file's shape and every
  claim the repository can answer for itself — the policy lists, the runtime
  floors in the manifests and the ADR, the SDK majors, the bundle, the registry
  file, the installed commands, and the runtime-specific settings
  `contract/config.json` records (today the AES entries of `runtimeChoices`) —
  and that
  [docs/compatibility.md](docs/compatibility.md#runtime-differences) lists
  every entry.
- **The docs no longer promise more interchangeability than CI enforces, or
  less.** "Interchangeable, nothing depends on the choice" is now "first-class,
  apart from the declared differences" in the README, `docs/install.md`,
  `docs/architecture.md` and both package READMEs. The Python README no longer
  implies the `.mcpb` bundle is Python. `docs/install.md` stops recommending the
  Python executable unconditionally when SECURITY.md recommends Node for
  untrusted networks. SECURITY.md's supported versions named only the npm
  release; security fixes ship in both. And `docs/compatibility.md` and
  `docs/certificates.md` still said the server certificate could not be pinned —
  `OPCUA_SERVER_CERT` pins it, and the end-to-end suite checks that on both
  runtimes. The README's overview also said thirteen tools; there are fifteen.
- **A runtime divergence has somewhere to go.** The bug template gains a *both
  runtimes, behaving differently* option, and CONTRIBUTING states the rule: a
  behaviour change lands in both runtimes in the same PR, or is declared.

### Not done (tracked on #134)
- A CA trust store with revocation checking. python-opcua has no server
  certificate validation to build one on, and a Node-only mechanism would break
  the parity both runtimes keep; pinning is exact-match, so a revoked or
  URI-mismatched certificate is handled by replacing the pin.

## [0.5.1] — 2026-09-22

0.5.0 was tagged but reached neither npm nor PyPI. Both registry jobs failed, for
the same underlying reason: the repository moved to the IndustriAgents
organisation on 2026-09-20, and while #125 updated the links in the prose, it
touched no package manifest and no publishing account. 0.4.1 had shipped two days
before the move, so 0.5.0 was the first release that could discover this.

This release is 0.5.0 plus the fix, so **0.5.1 is the first published release of
the 0.5 line** and the 0.5.0 notes below describe what is in it. Tags in this
repository are immutable by ruleset, which is why this is a new version rather
than a re-tag.

### Fixed
- **npm refused the upload because the package disagreed with its own
  provenance.** `npm publish --provenance` has GitHub attest which repository
  built the tarball, and npm checks that against the `repository.url` the package
  declares:

      422 Unprocessable Entity — Error verifying sigstore provenance bundle:
      package.json: "repository.url" is
      "git+https://github.com/midhunxavier/OPCUA-MCP.git", expected to match
      "https://github.com/IndustriAgents/OPCUA-MCP" from provenance

  The stale URL had been harmless for as long as it was only a link — GitHub
  redirects a transferred repository, so nothing else noticed. Signing turned it
  into a claim that had to be true. `repository.url`, `homepage` and `bugs` now
  name the new org, as do the `homepage`, `documentation`, `support` and
  `repository` fields of `mcpb/manifest.json`, which are the links Claude Desktop
  shows for an installed bundle.

  Unchanged, deliberately: the author URL, which is a personal profile rather
  than the repository, and `mcpName` / `server.json`'s
  `io.github.midhunxavier/opcua`. That pair is the MCP Registry namespace, which
  proves ownership by matching the published package's `mcpName` — an identity,
  not a link, and renaming it is a re-registration rather than a fix.

- **PyPI rejected the token because the trusted publisher named the old owner.**
  Trusted publishing matches the workflow's OIDC claims against a publisher
  registered on the project, and those claims carry the new owner literally, with
  no redirect:

      invalid-publisher: valid token, but no corresponding publisher
        repository: IndustriAgents/OPCUA-MCP
        repository_owner: IndustriAgents
        environment: pypi

  Fixed by re-registering the trusted publisher on PyPI against
  `IndustriAgents/OPCUA-MCP`. Account configuration, with nothing to change in
  the repository — recorded here because it is invisible from the source tree and
  is exactly the kind of thing the next org move will break again.

No change to either server. Both runtimes are byte-for-byte the 0.5.0 code.

## [0.5.0] — 2026-09-21

Seventeen issues from two architecture reviews, closed across four PRs. The
theme is the gap between what this server *claimed* and what it could *know*: a
write re-sent after an outcome nobody observed, an audit trail that could not say
who or which plant, a policy that authorised nodes but never values, a browse
that named a node without saying what it is, and a server that could not report
its own health. Plus the OPC UA coverage those reviews ranked highest —
engineering units and ranges, deadband filtering, the rest of the Alarms &
Conditions operator workflow, server diagnostics, type definitions and historical
events.

### Fixed
- **On Python 3.10, a session that died mid-request was not recognised as one.**
  python-opcua waits for every response with `future.result(timeout)`, so
  `concurrent.futures.TimeoutError` is exactly what a dying session raises — and
  on 3.10 that is *not* the builtin `TimeoutError`. The two became the same
  object only in 3.11, and on 3.10 it is not an `OSError` either, so none of the
  types the dead-session check names matched it; its `str()` is empty, so the
  text markers could not catch it afterwards either. The call came back as
  `Failed to read nodes: ` — no reason at all — and **no reconnection happened**,
  leaving the server dead until someone restarted it. That is the precise failure
  the classification exists to prevent, on the oldest supported Python, and it is
  invisible to anyone developing on 3.11+. Found by CI's version matrix. The tool
  bodies now also report an exception's type when it carries no message, so a
  message-less failure can never again be reported as nothing at all. The Node
  runtime is unaffected.

### Changed
- **Both runtimes keep their state on an instance, not in the module** (#116). The
  contract pins the tool surface and the tests pin the semantics; nothing pinned
  the *structure*, and that is where the two halves had drifted. Node built its
  connection, tools and policy in `index.ts` and passed them down, while Python
  kept the same state in module globals — so the Python server could not be
  instantiated twice in one process and the Node one nearly could. Python's state
  now lives on a `ServerState` owned by the server, and tool registration moved
  into `create_server()`, because a module-level `@mcp.tool` decorator binds a
  tool to whichever instance existed at import: with two instances the second
  would have had no tools. Node's two memoised singletons became plain factories,
  with one policy constructed and passed to both halves.

  No behaviour change — but one hazard worth naming, because the refactor could
  have introduced it: the connection re-binds the policy's namespace mapping on
  every reconnect and authorization reads it, so the two must be the *same*
  object, or an `nsu=<uri>;i=…` write allowlist entry is bound on one policy and
  resolved against another that has never seen a NamespaceArray. That held before
  only because both callers received one memoised instance. The `nsu=` form now
  has end-to-end coverage on both runtimes — it had none, because the bundled mock
  created its nodes at a bare namespace index without publishing a URI for it, so
  there was no URI such an entry could name. The mock registers one now, which
  also makes its NamespaceArray honest about the namespace its own node ids use.

### Added
- **`read_event_history`: look backwards at an alarm burst** (#117).
  `subscribe_events` only sees what arrives after it subscribes, which is the
  right shape for watching a plant and the wrong shape for the question people
  actually ask — what fired overnight, what happened in the ten minutes before
  the line stopped. By the time anyone asks, those events are gone. OPC UA Part
  11 §6.5.2 answers it with `ReadEventDetails`, and a server that historises its
  events already holds what is being asked for. The records are the same ones
  `read_events` returns, deliberately: both paths send the same select clauses
  and run the same decoder, so an alarm looks identical whether it was watched
  live or recovered afterwards. Gated on `AccessHistoryEventsCapability`
  (`ns=0;i=11194`), which is a different node and a different answer from the one
  `read_opcua_history` uses — Part 11 §5.4 lets a server keep values without
  keeping events. The bundled mock learned to keep its events so both sides of
  that gate are covered against real servers: python-opcua stores nothing unless
  the event source declares `GeneratesEvent`, and has no
  `AccessHistoryEventsCapability` node at all until one is created.
- **A browse now says what each node *is*** (#120). A browse record named a node,
  its class and its parent, which is enough to walk an address space and not
  enough to understand one: every alarm, every pump and every folder came back as
  `Object`, and every reading as `Variable`. `type_definition` now carries the
  node's `HasTypeDefinition` — `AnalogItemType` for a tag that publishes a unit
  and a range (so it composes with #110), a subtype of `AlarmConditionType` for
  something `act_on_alarm` applies to. That reference is non-hierarchical, so the
  traversal's own browse never saw it; it is fetched as one batched browse for
  the whole result, chunked, rather than one round trip per node. A node with
  more than one type definition — which OPC UA Part 3 §4.3 does not allow, and
  which this project's own mock server had — reports `null` rather than letting
  the two runtimes pick different references. Decoding structured values
  (`ExtensionObject`) is a separate problem and is not part of this.
- **`get_server_status` reports the server's own diagnostics** (#121). The status
  report covered this client's view of the connection and said nothing about the
  server's load, so "the plant server is slow" and "the plant server is refusing
  us" looked identical from the agent's side. `diagnostics` now carries OPC UA
  Part 5's `ServerDiagnosticsSummary` (`ns=0;i=2275`) — twelve counters that tell
  those apart: `rejected_session_count` and `security_rejected_session_count`
  separate a server turning connections away from wrong credentials,
  `cumulated_session_count` far above `current_session_count` is a client
  reconnecting in a loop, and `current_subscription_count` against
  `publishing_interval_count` shows how many subscriptions share a cycle. Part 5
  makes diagnostics optional, so the field is nullable and `null` means "this
  server does not say" rather than zero — the two mock servers differ exactly
  there, so both branches are covered against a real server. It is read in the
  same batch as the status and the namespace array, so it costs no extra round
  trip. Discovery (`FindServers`/`GetEndpoints`) is deliberately not part of this:
  it is a network scanner, and it needs its own access class and endpoint
  allowlist before it belongs in a tool an agent can call.
- **`act_on_alarm`: the rest of the operator workflow** (#119). `acknowledge_alarm`
  implemented the first half of OPC UA Part 9 §5.5's acknowledge→confirm handshake
  and nothing else, so an agent could say "I have seen this" and then had no way to
  say "I have dealt with it", to leave a note, or to do what an operator actually
  does with a chattering nuisance alarm. The new tool adds `confirm`, `comment`,
  `shelve`, `shelveFor` (self-limiting: the alarm returns whether or not anyone
  remembers to unshelve it) and `unshelve`. It is a second tool rather than a
  rename because merging would break every existing caller, but the two share one
  implementation and resolve their method through one table, which is the property
  the 17→13 consolidation was about. Suppress, Enable/Disable, Reset and Silence
  are deliberately out: they configure the alarm system rather than respond to an
  alarm, and an agent switching an alarm off is not a feature.
- **Subscriptions can filter where the values are** (#118). `subscribe_opcua_nodes`
  took three timing arguments and no filter, so a noisy analogue tag filled the
  default 20-record ring with sensor jitter in about a second — the agent read it
  back, saw nothing but noise, and had spent one of the server's 200
  subscriptions to get it. `deadband_type` (`none`/`absolute`/`percent`),
  `deadband_value` and `data_change_trigger` now pass OPC UA Part 4 §7.22's
  `DataChangeFilter` through to the monitored item, so the discarded values never
  leave the server. `percent` is a percentage of the node's `EURange`, which is
  why this composes with #110; a node publishing no range is refused rather than
  quietly given an absolute deadband. The default trigger is `statusValue`, not
  OPC UA's own `status`, and a request that asks for nothing special sends no
  filter at all. Every record reports the filter in force.
- **`OPCUA_AUDIT_FILE`: somewhere durable for the control audit trail to go**
  (#113). It only ever went to stderr, which for a stdio subprocess launched by
  an MCP client is that client's rotating log — not a compliance artifact, not
  integrity-protected, and not shippable by policy. The file is append-only, one
  JSON object per line, written synchronously per record; a restart appends and
  never truncates; a file that cannot be opened stops the server rather than
  falling back and leaving an operator believing they had a durable record.
  stderr is still written either way. Rotation, syslog and the Windows Event Log
  are deliberately out: a file a collector tails is the seam.
- **Audit records now say which plant, which session, and on whose behalf**
  (#113). The record named the call, the profile, the tool, the decision and the
  targets — and nothing else, so "a write to `ns=2;i=5` was allowed" was not
  interpretable six months later, given that `ns=2;i=5` names a different
  physical node after a server reloads its namespaces in a different order. Every
  record now carries `endpoint`, `session` and `operator`
  (from `OPCUA_OPERATOR_ID`, a deployment label rather than an identity: this
  server has no notion of who is calling, and a name nothing verified would be
  worse than none).
- **Every reading now says what its number means** (#110). `AnalogItemType`
  publishes `EngineeringUnits`, `EURange` and `InstrumentRange` — OPC UA Part 8
  §5.3 introduces the first by citing the Mars Climate Orbiter — and nothing here
  read them, so a model handed `51.75` could not tell °C from PSI from %, or a
  reading from a trip. `resultShapes.nodeValues` gains an `engineering` field
  carrying all three (`null` for a node that publishes none, which is most). It
  costs two extra round trips for a whole batch on a cold cache — one
  `TranslateBrowsePathsToNodeIds`, one `Read` — and none on a warm one; the cache
  is dropped when the session is replaced.
- **A write outside the range the plant itself published is refused** (#110),
  before anything is sent. This is a safety bound the equipment declared rather
  than one a human retyped into a policy file, and it is the only value bound that
  exists on a deployment with no policy file at all.
  `OPCUA_ALLOW_OUT_OF_RANGE_WRITES=true` turns it off for the deployments —
  commissioning, forcing a value during a test — that write outside normal
  operation on purpose.

### Security
- **A refusal and a plant rejection read the same** — and one of them no longer
  does. A rebuild that failed *during* recovery reported whatever the client
  library said (`[Errno 61] Connection refused`) while the same outage a call
  earlier had been described as "Not connected to the OPC UA server at …: … Call
  get_server_status for details". One server, one event, two stories depending on
  where in the request it happened to notice. Found by the end-to-end test added
  for #112.
- **The policy authorised nodes and never values** (#109). `writable_nodes` asked
  one question: is this node on the list? An allowlisted setpoint then accepted
  any number the variant codec would encode, so a model that correctly identified
  the right node and hallucinated `9999` instead of `99.9` was fully authorised —
  the codec range-checks integers and refuses a lossy Int64, but that is *type*
  safety and `9999` is a perfectly good Double. A `writable_nodes` entry may now
  be an object carrying `min`, `max`, `enum` or `max_change`; a bare node id stays
  legal and means exactly what it meant. `min`, `max` and `enum` are decidable
  from the call alone and are refused before the network is touched; `max_change`
  is a bound on the *move* and is checked in the write path against the read the
  type inference already does. Both these and the server's own `EURange` apply, so
  a policy file can only ever narrow what the equipment allows, and one
  out-of-bounds value rejects the whole batch as one forbidden target already did.

- **A write was automatically re-sent after an outcome nobody knew** (#106). Both
  runtimes read `annotations.idempotentHint` as their transport retry policy, and
  `write_opcua_nodes` carries `idempotentHint: true` — correctly, because writing
  99.9 twice leaves 99.9, which is what that annotation tells the *model*. It is
  not what it tells the transport. OPC UA Part 4 §5.11.4 lets a Write partially
  succeed, leaves rollback to the client and defines no operation order, so a
  session that died before the response arrived never proved the write had not
  landed — and the server re-sent it, a second physical actuation on a guess. The
  contract now carries a server-private `retryPolicy` per tool: reads `resend`,
  monitor tools `reconnectOnly`, and every control tool `uncertainOutcome`, which
  rebuilds the connection and then says plainly that the request may or may not
  have reached the plant, naming what it was aimed at so an operator can read the
  targets back. `idempotentHint` is unchanged and still means what MCP says it
  means.
- **A re-sent request was never re-authorized** (#105). Both dispatchers
  authorized a call, then ran it again on a *different* session after a reconnect
  — and `reconnect()` re-reads the server's `NamespaceArray`, because a restarted
  server may have loaded its namespaces in a different order, which is the entire
  reason the `nsu=` allowlist form exists. An `ns=2;i=5` authorized against one
  namespace map could be re-sent against another and reach a different physical
  node. Authorization now runs again inside the retry, after the namespaces are
  re-bound, and every audit line carries an `attempt` number so a call that
  reached the plant twice is two records rather than one.

### Fixed
- **A whole outage's worth of callers each ran their own backoff** (#111). The
  Python connection held its lock across the entire retry loop, sleeps included,
  so every concurrent tool call waited out the full budget (7s by default, 32s
  with `OPCUA_RECONNECT_MAX_RETRY=-1`) before it was even told the server was
  down. Serialising the callers was right; making them sit through the sleep was
  not. `reconnect` now claims the attempt under the lock and does the teardown,
  the backoff and the rebind without it, and a caller that arrives mid-rebuild
  takes that attempt's answer rather than queueing another — the last of N
  callers would otherwise wait N budgets to be told what the first already knew.
- **One outage rebuilt the connection once per caller** (both runtimes). Every
  in-flight call fails on the same dead session and every one of them asks for a
  rebuild; the ones arriving after the first finished started another, tearing
  down a session that was working and re-attaching every subscription on it for
  nothing. A caller now names the session its operation died on, and one that has
  already been replaced needs no second rebuild.
- **Dead-session classification was 23 hand-transcribed strings, guarded by a
  test that could not fail** (#112). The test parametrised over the same constant
  it was checking, so it passed by construction — and the failure it existed to
  catch is a client library rewording a message and silently disabling
  reconnection, leaving a server dead until someone restarts it. The contract now
  separates the three kinds of evidence: 14 OPC UA status codes *by name*, which
  each runtime resolves against its own library's enum (so a name that stops
  existing fails a test, and the numbers come from the spec, so the two runtimes
  provably agree); 6 errno codes, fixed by the operating system; and 3 phrases,
  the fragile part, kept small. Where an error carries a status code it is matched
  on the number. And the check that actually fails when reconnection stops
  working is new: an end-to-end test that takes the plant away while the session
  still looks alive, so the failure arrives from inside a request.
- **Argument validation accepted unknown properties** (#114). No input schema
  forbade extras, so `{"node_clas": "Variable"}` was accepted and browsed with the
  default node class — a misspelled *optional* argument changed behaviour instead
  of producing a refusal the model could correct from. The shared validator gained
  `additionalProperties`, `enum` (`node_class` and `data_type` are fixed sets; an
  unknown node class used to match nothing and come back as an empty list,
  indistinguishable from a subtree that really is empty) and `minimum` (every
  numeric argument is floored at zero; a negative was silently clamped). Ceilings
  stay clamps: they are documented caps on how much work one call may ask for.
- **Two concurrent reconnects could tear down each other's fresh session**
  (#107). The Node runtime's `connect()` was single-flight but `reconnect()` was
  not, so two calls recovering from the same outage could interleave as: A tears
  down, A opens a new session, B tears down and closes the session A had just
  opened and was about to return. A then believed it held a live session and
  every call after it failed. The whole teardown → open → rebind → re-establish
  sequence is now claimed once, and concurrent callers await the same rebuild.
- **A cold start refused the history tools without asking the server** (#108).
  Both dispatchers checked a tool's capability gate before establishing a
  connection, and the capability map is filled in by the reconnect callback — so
  a process that started while the plant was unreachable still held its startup
  defaults, and `read_opcua_history` was refused as "OPC UA server advertises
  none of: history" with no connection ever attempted. Unknown is not absent.
  `tools/call` now connects first and checks second; `tools/list` still does no
  network I/O.

### Documentation
- **The contract header's factual claims are now checked** rather than only
  corrected. #122 fixed the three that had drifted — FastMCP (renamed in SDK 2.x),
  signature-derived schemas (#81 changed that), and a test path that had moved —
  and `tests/unit/test_contract.py` now asserts the checkable parts, so prose that
  names a file has to name one that exists and prose that names a runtime has to
  name the one the server imports.
- **`docs/architecture.md` now says *why* there are two runtimes.** #122 added the
  missing clause to "users pick whichever runtime their stack already has"; this
  says what that sentence was standing in for, because it is what decides where
  effort goes. Python is where the OPC UA and industrial-data ecosystem lives, and
  `python-opcua` being unmaintained makes a second independent implementation
  insurance rather than redundancy — which argues for pushing decisions into the
  contract so each runtime shrinks toward a thin adapter, an argument "pick your
  stack" does not make.
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

[Unreleased]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/IndustriAgents/OPCUA-MCP/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/IndustriAgents/OPCUA-MCP/releases/tag/v0.1.2
