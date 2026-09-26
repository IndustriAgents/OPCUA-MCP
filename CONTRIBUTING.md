# Contributing to OPC UA MCP

Thanks for your interest in contributing! This repo provides **two MCP servers**
(Python and TypeScript/Node) that bridge AI assistants to OPC UA servers, plus a
**mock industrial OPC UA server** for local development and testing.

- **[docs/architecture.md](docs/architecture.md)** — how the pieces fit, and the invariants to preserve
- **[docs/testing.md](docs/testing.md)** — how to test (automated suite, MCP Inspector, AI agents)
- **[docs/examples.md](docs/examples.md)** — per-tool inputs/outputs and node-ID reference
- **[docs/dependency-policy.md](docs/dependency-policy.md)** — supported runtimes and dependency ranges, how both ends are tested, and what a dependency change needs before it merges

## Repository layout

| Path | What it is |
|------|------------|
| `packages/mock-server/` | Mock "Industrial Control System" OPC UA server (:4840; advertises no aggregate functions, on purpose) |
| `packages/mock-server-aggregate/` | Aggregate-capable mock (:4841), backing the aggregate tests |
| `packages/mock-server-alarms/` | Alarms & Conditions mock (:4842), backing the alarm tests |
| `packages/server-python/` | **Python** MCP server (`mcp`/`MCPServer` + `opcua`/FreeOpcUa), a `src/` package |
| `packages/server-node/` | **Node** MCP server (TypeScript + `@modelcontextprotocol/sdk` + `node-opcua-client`) |
| `tests/` | End-to-end pytest suite driving both servers via the `mcp` SDK |
| `docs/` | Usage docs (`architecture.md`, `examples.md`, `install.md`, `testing.md`); `archive/` holds executed plans |
| `examples/` | Standalone demo scripts (not part of any package) |

```
AI assistant / MCP client  ──stdio──►  MCP server (Python OR Node)  ──OPC UA/TCP──►  mock server :4840
```

The two MCP servers share a single tool contract ([`contract/tools.json`](contract/tools.json)): the Node server builds its `tools/list` from it and the Python server reads descriptions and capability node IDs from it, so they cannot drift (`tests/e2e/test_contract_parity.py` enforces this). The same file defines the **resource** surface, under `resources` — both servers build their `resources/list` from it.

The **configuration** surface has its own contract, [`contract/config.json`](contract/config.json): every `OPCUA_*` variable, its type, default, secrecy and which runtimes read it. To add or change a setting, edit it there first, implement it in **both** runtimes, then run `npm run config:generate` in `packages/server-node` — that rewrites the settings form in `mcpb/manifest.json`, the environment variables in `server.json` and the configuration tables in `docs/configuration.md` and the package READMEs, which are generated and must not be edited by hand. `tests/unit/test_config_schema.py` fails if a runtime reads a variable the schema does not declare (or the reverse), or if a parser disagrees with a declared choice, minimum or default; CI runs `npm run config:check` for the generated files.

### Generated documentation

Facts the contracts already hold are generated into the docs rather than copied
by hand, because every hand copy drifted (#149). `npm run config:generate` (in
`packages/server-node`) rewrites them all, and `npm run config:check` — a step of
the CI `lint` job — fails if any is out of date:

| What | Where | Source |
|---|---|---|
| Tool table, count, access classes, annotations, capability gates | `docs/tools.md`, both package READMEs, `docs/examples.md`; a summary by access class in `README.md` | `contract/tools.json` |
| Configuration reference, grouped by category | `docs/configuration.md`, both package READMEs (each narrowed to what its runtime reads) | `contract/config.json` |
| The release version | `ROADMAP.md`, `mcpb/manifest.json`, `server.json`, both `pyproject.toml`s, `package-lock.json`, `uv.lock` | `packages/server-node/package.json` |

In Markdown, only what sits between a `<!-- BEGIN GENERATED: name ... -->` /
`<!-- END GENERATED: name -->` pair is generated. Edit the prose around a block
freely; edit inside one and the next run puts it back. To change a generated
table, change its source and regenerate. After a rebase that conflicts inside a
block, take either side and regenerate — the result depends only on the
sources. The generator refuses a file that has lost a marker pair it expects, and
`docs/examples.md` must keep a heading naming each tool in code
(`` ### `foo` ``), because the tool index links to it. The rest is checked by
`tests/unit/test_docs.py`: every relative link and every link into this
repository resolves, heading anchors included, and no live document states a
tool count other than the contract's.

**Both runtimes are first-class** ([ADR 0001](docs/adr/0001-two-first-class-runtimes.md)). A change to what either server does lands in **both**, with tests for both, in the same PR — a rule both runtimes must agree on goes in a shared table under `tests/fixtures/` driven by both unit suites, rather than being asserted twice. The only exception is a difference the ADR allows (an extra capability, a stricter input rule, a distribution channel — never a different tool surface or a weaker security baseline): declare it in [`contract/runtime-differences.json`](contract/runtime-differences.json) (a runtime-specific setting is also marked in `contract/config.json` with `runtimes` or `runtimeChoices`, and a test keeps the two in step), add it to the table in [docs/compatibility.md](docs/compatibility.md#runtime-differences), and say so in the changelog.

## Prerequisites

- **Python 3.10+** and [`uv`](https://docs.astral.sh/uv/)
- **Node.js 22.13+** and **npm**
- No OPC UA broker needed — the mock server is included.

## Local development

Start the mock server first (it's the data source for everything else):

```bash
uv sync --all-packages          # one-time: set up the workspace env
uv run --no-sync opcua-mock-server
# listens on opc.tcp://0.0.0.0:4840/freeopcua/server/  (history enabled)
```

### Python MCP server
```bash
OPCUA_SERVER_URL=opc.tcp://localhost:4840/freeopcua/server/ \
  uv run --no-sync opcua-mcp-server
```

### Node MCP server
```bash
cd packages/server-node
npm install
npm run build        # compiles src/*.ts -> build/, stages contract + version
OPCUA_SERVER_URL=opc.tcp://localhost:4840/freeopcua/server/ node build/index.js
```

Both servers read the endpoint from `OPCUA_SERVER_URL` (default
`opc.tcp://localhost:4840`). They speak MCP over **stdio**, so keep `stdout`
clean — write all logs to `stderr`.

## Running the tests

Three tiers — use the narrowest one that covers your change:

```bash
uv sync --all-packages                        # one-time workspace setup

cd tests
uv run --no-sync pytest unit/                 # <1s, no server needed
uv run --no-sync pytest                       # unit + e2e (~50s)
uv run --no-sync pytest -m smoke smoke/       # downloadable artifacts (~60s)

cd ../packages/server-node
npm run build && npm test                     # Node unit tests
```

The **smoke** tier builds every artifact a user can download — npm tarball, Python
wheel, the `.mcpb` bundle and both single-file executables — installs them in
isolation, and drives them over MCP. It is the only tier that can see packaging
faults, unbounded dependencies, or a bundling change that breaks once
`node_modules` is no longer on disk. The first two have shipped broken releases
here before, so run it before any release. Building the executables needs
PyInstaller: `uv sync --all-packages --group packaging`.

See [tests/README.md](tests/README.md) for details and selectors
(`-k "[python]"` / `-k "[node]"`). For manual testing with the MCP Inspector or an AI
agent, see **[docs/testing.md](docs/testing.md)**.

## Adding a new MCP tool

The tool surface is defined once in [`contract/tools.json`](contract/tools.json); both servers derive from it, and `tests/e2e/test_contract_parity.py` fails if they diverge. To add a tool `foo`:

1. **Contract** (`contract/tools.json`): add an entry under `tools` with its `name`, `description` (its first sentence is the tool's one-line summary in the generated tables), `inputSchema` (JSON Schema), `accessClass`, `annotations`, `retryPolicy`, and `capabilities` (`[]`, or the entries of `capabilities` it depends on — any one of them is enough). A `control` or `alarm-action` tool also needs a `guard`, or the policy denies it.
   If the tool returns structured data rather than free text, give it a `resultShape` naming an entry under `resultShapes` — reuse an existing shape where one fits. Both servers must then emit that shape byte-comparably; a client that has learned one server's output has to be able to read the other's, and `tests/e2e/test_contract_parity.py` checks the real output against the shape.
2. **Node** (`packages/server-node/src/tools.ts`): add a `case "foo"` to the `callTool` switch and implement the handler method. You do **not** edit `listTools` — it is generated from the contract. Run `npm run build` (this also stages the contract and version into `build/`).
3. **Python** (`packages/server-python/src/opcua_mcp_server/server.py`): add a function `foo` with typed args (`MCPServer` derives the input schema from them — keep it matching the contract) and `ctx: Context`, and add its name to `TOOL_NAMES`, which `create_server` registers with the contract's description. Gating on a capability the contract already declares needs no code of its own: `list_tools` hides a tool whose `capabilities` the connected server does not advertise. A new capability also needs a probe on both runtimes.
4. **Test**: add an end-to-end test in `tests/e2e/test_mcp_e2e.py` (it runs against both servers). The contract-parity test will automatically check that both servers advertise the new tool with the contract's description and parameters; if the tool declares a `resultShape`, assert the returned records against it with `assert_matches_result_shape`.
5. **Document it** in `docs/examples.md` (the central per-tool reference) under a heading naming it in code (`` ### `foo` ``), then run `npm run config:generate` in `packages/server-node` to add it to the generated tool tables and counts.

Adding a **resource** follows the same path through the `resources` key: a `uri`,
`name`, `description` and `mimeType`, plus a `body` naming the `resultShape` its
document carries and the key it sits under. Node serves it from `listResources` /
`readResource` in `src/tools.ts`; Python registers it with `@mcp.resource` in
`server.py`, and note that `MCPServer` refuses to inject a `Context` into a
*static* resource — which is why the subscription manager is module-level state
rather than something held in the lifespan context.

## Code style

Style is enforced by tooling, not by review. CI runs all of the below in a
`lint` job; run them locally before pushing:

```bash
uv run ruff check .            # Python lint  (--fix to autofix)
uv run ruff format .           # Python format
cd packages/server-node
npm run format:check           # Prettier     (npm run format to autofix)
npm run typecheck              # tsc --noEmit
```

Beyond what the tools check:

- **Python**: type-hint tool signatures — `MCPServer` derives the input schema
  from them, so a wrong annotation is a wire-protocol bug, not a style nit.
- **Python**: raise `ToolError` for a failure the caller should see. The SDK
  forwards its message and withholds every other exception's as a crash, so a
  bare `raise` turns a diagnosable error into `Error executing tool <name>`.
- **Never write to `stdout`** except via the MCP transport; stdout carries the
  JSON-RPC stream and stray output corrupts it. Use `print(..., file=sys.stderr)`
  in Python; the Node server already redirects stray `console.log` to `stderr`.
- Broad `except Exception` in a tool handler is intentional and allowed — return
  a readable error to the model rather than tearing down the transport. (This is
  why the `BLE` ruleset is not enabled.)
- Convert/validate inputs explicitly (e.g. date strings → `Date`) and return
  clear error messages.

## Commit & PR conventions

- Branch off `main`; keep commits focused with descriptive messages.
- Run the suite (`uv sync --all-packages`, then `cd tests && uv run --no-sync pytest`) before opening a PR.
- Reference related issues/PRs (e.g. "Fixes #1").
- If a change was AI-assisted, keep the `Co-Authored-By:` trailer.
- PRs from forks: enable **"Allow edits by maintainers"** so reviewers can rebase.

## Changing a dependency

Read **[docs/dependency-policy.md](docs/dependency-policy.md)** first. In short:
every runtime dependency of both published packages carries a floor CI tests and
a ceiling below the next major (`>=floor,<next-major` in Python, `^floor` in
Node), `tests/unit/test_dependency_ranges.py` holds the manifests and the
policy's table to that, and every runtime dependency counts as security-sensitive,
so its update merges on the full E2E suite, never on unit tests alone.
Editing a range means relocking (`uv lock`, or `npm install --package-lock-only`
in `packages/server-node`) and updating the table in the policy.

Deprecation warnings fail the tests. Fix one raised by our own code; for one
raised inside a dependency, add an entry to
`tests/fixtures/deprecation-allowlist.json` naming the issue, an owner and when
it can be removed.

## Releasing

See **[docs/releasing.md](docs/releasing.md)**. Releases are tag-triggered and
gated on the full suite plus the artifact smoke tests. Two workflows run off the
tag: `publish.yml` ships to npm and PyPI, and `release.yml` builds the `.mcpb`
bundle and the per-platform executables and attaches them to the GitHub release.

The version has one source, `packages/server-node/package.json`. Set it there
and run `npm run config:generate` in `packages/server-node`: that stamps it into
`mcpb/manifest.json`, both `version` fields of `server.json`, both
`pyproject.toml`s, `package-lock.json`, `uv.lock` and the roadmap.
`npm run config:check` and `tests/unit/test_version_manifests.py` fail if any
copy drifts.

## Security note

The servers **default** to `SecurityPolicy.None` / `MessageSecurityMode.None`
for local development against the mock. Do **not** use that default against
production OPC UA systems — configure `OPCUA_SECURITY_POLICY`,
`OPCUA_CLIENT_CERT`/`OPCUA_CLIENT_KEY` and credentials
([Configuration](docs/configuration.md)), pin the server with
`OPCUA_SERVER_CERT`, and note the gaps listed in [SECURITY.md](SECURITY.md)
(notably that there is no CA trust list for the server certificate, only
pinning, and that a written value is bounded only where the node publishes an
`EURange` or the policy file sets a bound).

Both runtimes parse those variables in one place — `src/security.ts` and
`src/opcua_mcp_server/security.py` — with the same defaults and the same error
wording. A change to one belongs in the other, and the unit suites on both sides
assert the shared messages. Parity extends past the parsing: the two must also
reach a server as the same identity, which is why the Python side derives the
session's ApplicationUri from the client certificate as node-opcua does. How
users make that certificate is [docs/certificates.md](docs/certificates.md).
