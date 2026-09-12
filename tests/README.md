# OPC UA MCP — Test Suite

Three tiers, fastest first. Pick the narrowest one that covers your change.

| Tier | Directory | Needs | Time | What it is for |
|------|-----------|-------|------|----------------|
| **unit** | `unit/` | nothing (a built Node server for the `--install` parity tests) | ~5s | Pure logic: ISO-8601 parsing, contract invariants, version manifests, security config, `--install` |
| **e2e** | `e2e/` | mock OPC UA servers + built Node server | ~70s | Drives both real servers over stdio via the `mcp` client SDK, unsecured and secured |
| **smoke** | `smoke/` | npm + uv (+ PyInstaller for the executables) | ~60s | Builds every downloadable artifact — npm tarball, wheel, `.mcpb` bundle, single-file executables — and drives them |

```bash
uv run --no-sync pytest unit/         # fast inner loop
uv run --no-sync pytest               # unit + e2e (smoke is deselected by default)
uv run --no-sync pytest -m smoke smoke/
```

The Node server has its own unit tests, run separately:

```bash
cd packages/server-node && npm run build && npm test
```

**Why the smoke tier exists:** everything else runs from the source tree, where
relative paths happen to resolve and dependencies come from `uv.lock`. Users get
a tarball, a wheel, a bundle or a binary. That gap has shipped real bugs — a wheel
that raised `FileNotFoundError` on import, and an unbounded `mcp` dependency that
resolved to a breaking major on any fresh install. Both were invisible to the e2e
suite.

The gap is widest for the two newest artifacts, which is why they are built here
rather than trusted:

| Artifact | What only this tier can catch |
|---|---|
| `.mcpb` bundle (`test_artifacts.py`) | The whole dependency tree is bundled into one file with the contract inlined, so there is no `node_modules` and no `contract.json` on disk. node-opcua needs `require`, `__filename` and `__dirname` supplied by hand to survive that. |
| Executables (`test_binaries.py`) | An embedded interpreter, no packaging metadata in the usual place, and a `--install` that must name the binary alone — handing a frozen app `-m opcua_mcp_server` writes a config entry that fails every launch. |

The executables cannot be cross-compiled, so this tier only ever covers the
platform it runs on. `.github/workflows/release.yml` builds and checks the other
two. PyInstaller comes from an opt-in dependency group:
`uv sync --all-packages --group packaging`.

## What the e2e tier covers

Every test runs against **both** server implementations.

| Test | What it verifies |
|------|------------------|
| `test_lists_core_tools` | All 7 core tools are advertised |
| `test_history_tool_exposed_when_supported` | History tool appears because the mock enables history |
| `test_aggregate_tool_hidden_when_unsupported` | Aggregate tool is **hidden** (mock advertises no aggregate functions) — capability gating |
| `test_aggregate_tool_exposed_when_supported` | Aggregate tool **appears** against the aggregate-capable mock |
| `test_aggregate_average_values_are_correct` | `Average` over a known ramp advances by exactly one interval per bucket |
| `test_aggregate_default_end_time_is_utc` | Omitting `end_time` does not overshoot the window on a non-UTC host (#24) |
| `test_aggregate_rejects_unknown_function` | An unsupported aggregate name is rejected, listing what the server offers |
| `test_read_single_node` | `read_opcua_node` returns a value |
| `test_read_multiple_nodes` | `read_multiple_opcua_nodes` returns all requested nodes |
| `test_get_all_variables` | `get_all_variables` discovers the address space |
| `test_browse_children` | `browse_opcua_node_children` lists the four folders |
| `test_write_numeric_node` | Writing a `Double` actuator succeeds |
| `test_write_boolean_node` | Writing a `Boolean` node with `"true"` succeeds (bool-handling regression) |
| `test_call_method_start_then_stop` | `call_opcua_method` drives `StartProduction`/`StopProduction` and `SystemMode` reacts |
| `test_read_history` | The history tool (`read_history_opcua_node`) returns timestamped records |
| `test_refuses_to_start_without_the_certificate_the_policy_needs` | A security policy with no certificate exits with the same `Configuration error: …` on both runtimes |
| `test_reads_and_writes_over_a_secured_connection` | Read/write work over Basic256Sha256, in `Sign` and in `SignAndEncrypt` |
| `test_the_password_never_reaches_the_logs` | `OPCUA_PASSWORD` appears nowhere in the server's stderr |
| `test_default_mode_is_sign_and_encrypt` | A policy with no explicit mode negotiates the strongest endpoint, not the weakest |
| `test_the_application_uri_comes_from_the_certificate` | With no `OPCUA_APPLICATION_URI`, both runtimes announce the certificate's own `subjectAltName` URI |
| `test_a_wrong_password_is_rejected` | Bad credentials yield `BadUserAccessDenied`, never a working session |
| `test_an_unsecured_client_cannot_use_the_secured_server` | With no security configured there is no endpoint to fall back to, and the server warns |

Both servers expose the history tool under the same name, `read_history_opcua_node`,
and only when the server advertises `AccessHistoryDataCapability`.

## Mock servers

Three are used, on purpose. Each is started by its fixture on a **fresh
ephemeral port** per session, so two checkouts can run the suite at once without
colliding (#46):

| Mock | Role |
|------|------|
| `packages/mock-server` (python-opcua) | Industrial address space, history, methods. Advertises **no** aggregate functions — this is what makes the capability-gating assertions meaningful. |
| `packages/mock-server-aggregate` (node-opcua) | Advertises aggregate functions and genuinely implements `ReadProcessedDetails`. Ramps `Temperature` (`ns=1;i=1001`) by +1.0/second so aggregates are verifiable arithmetically. |
| `tests/fixtures/secure_opcua_server.py` (python-opcua) | Offers **only** Basic256Sha256 endpoints and requires a username — the unsecured mocks cannot tell a working security config from an ignored one. Certificates are generated per session into a temp dir (`secure_pki`), never committed. `--check-client-uri` adds the ApplicationUri-against-certificate check that real servers make and python-opcua's does not. |

The main mock cannot serve aggregates even in principle: python-opcua answers
`ReadProcessedDetails` with `BadNotImplemented`.

The secured mock accepts any client certificate, because python-opcua's server
has no trust list. Real equipment does: a Siemens, Kepware or Prosys server
rejects an unknown client certificate until an operator moves it into its trusted
folder. That step, and vendor-specific certificate handling generally, can only
be verified by hand against the real server.

## Prerequisites

- `uv`, `node` (>=22.13, matching the server's `engines.node`), `npm`
- Set up the workspace once (from the repo root): `uv sync --all-packages`
- Build the Node server once: `cd packages/server-node && npm install && npm run build`
  (Node tests are **skipped** if `build/index.js` is missing).
- Install the aggregate mock once: `cd packages/mock-server-aggregate && npm install`
  (aggregate tests are **skipped** if its `node_modules` is missing, or on Node <20 —
  `node-opcua-aggregates` pulls dependencies that require it. This limits the test
  fixture only; the shipped Node server needs Node 22.13+).

## Running

```bash
cd tests
uv run --no-sync pytest -v
```

`pytest` runs the unit and e2e tiers; smoke is deselected by default via
`addopts = "-ra -m 'not smoke'"` because it builds and installs packages.

The suite starts its own mock OPC UA servers, each on a free port picked for the
session, and waits a few seconds for history to accumulate. To point it at a
server you manage yourself instead — one left running while iterating, or a real
device — set the endpoint explicitly; nothing is started, and arranging enough
history for the tests that read it back is then yours:

```bash
OPCUA_SERVER_URL="opc.tcp://localhost:4840/freeopcua/server/" uv run --no-sync pytest -v
OPCUA_AGGREGATE_SERVER_URL="opc.tcp://localhost:4841/UA/Aggregate" uv run --no-sync pytest -v
```

A standalone mock defaults to `:4840` (`uv run opcua-mock-server`, overridable
with `--endpoint`); the aggregate mock defaults to `:4841` (`npm start` in
`packages/mock-server-aggregate`, overridable with `AGGREGATE_MOCK_PORT`).

Select a single implementation:

```bash
uv run --no-sync pytest -v -k "[python]"
uv run --no-sync pytest -v -k "[node]"
```

The brackets matter: they match the parametrisation id. A plain `-k node` would
also match test *names* like `test_read_opcua_node`.

## Notes

- The mock server's method callbacks update internal state; OPC UA node values
  are propagated by its 1 Hz simulation loop, so tests poll (see
  `wait_for_node_value`) rather than reading immediately after a method call.
- Writes to sensor/actuator nodes may be overwritten within ~1s by the
  simulation loop; only the command variables (`StartProductionCommand`, …) and
  methods persist.
