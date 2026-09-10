# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/midhunxavier/OPCUA-MCP/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/midhunxavier/OPCUA-MCP/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/midhunxavier/OPCUA-MCP/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/midhunxavier/OPCUA-MCP/releases/tag/v0.1.2
