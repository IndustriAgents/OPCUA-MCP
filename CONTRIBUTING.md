# Contributing to OPC UA MCP

Thanks for your interest in contributing! This repo provides **two MCP servers**
(Python and TypeScript/Node) that bridge AI assistants to OPC UA servers, plus a
**mock industrial OPC UA server** for local development and testing.

- **[docs/architecture.md](docs/architecture.md)** — how the pieces fit, and the invariants to preserve
- **[docs/testing.md](docs/testing.md)** — how to test (automated suite, MCP Inspector, AI agents)
- **[docs/examples.md](docs/examples.md)** — per-tool inputs/outputs and node-ID reference

## Repository layout

| Path | What it is |
|------|------------|
| `packages/mock-server/` | Mock "Industrial Control System" OPC UA server (:4840; advertises no aggregate functions, on purpose) |
| `packages/mock-server-aggregate/` | Aggregate-capable mock (:4841), backing the aggregate tests |
| `packages/server-python/` | **Python** MCP server (`mcp`/`MCPServer` + `opcua`/FreeOpcUa), a `src/` package |
| `packages/server-node/` | **Node** MCP server (TypeScript + `@modelcontextprotocol/sdk` + `node-opcua-client`) |
| `tests/` | End-to-end pytest suite driving both servers via the `mcp` SDK |
| `docs/` | Usage docs (`architecture.md`, `examples.md`, `install.md`, `testing.md`); `archive/` holds executed plans |
| `examples/` | Standalone demo scripts (not part of any package) |

```
AI assistant / MCP client  ──stdio──►  MCP server (Python OR Node)  ──OPC UA/TCP──►  mock server :4840
```

The two MCP servers share a single tool contract ([`contract/tools.json`](contract/tools.json)): the Node server builds its `tools/list` from it and the Python server reads descriptions and capability node IDs from it, so they cannot drift (`tests/e2e/test_contract_parity.py` enforces this). The same file defines the **resource** surface, under `resources` — both servers build their `resources/list` from it.

## Prerequisites

- **Python 3.10+** and [`uv`](https://docs.astral.sh/uv/)
- **Node.js 18+** and **npm**
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

The tool surface is defined once in [`contract/tools.json`](contract/tools.json); both servers derive from it, and `tests/test_contract_parity.py` fails if they diverge. To add a tool `foo`:

1. **Contract** (`contract/tools.json`): add an entry under `tools` with its `name`, `description`, `inputSchema` (JSON Schema), and `capability` (`null`, or `"history"`/`"aggregate"` if it depends on a server capability).
   If the tool returns structured data rather than free text, give it a `resultShape` naming an entry under `resultShapes` — reuse an existing shape where one fits. Both servers must then emit that shape byte-comparably; a client that has learned one server's output has to be able to read the other's, and `tests/e2e/test_contract_parity.py` checks the real output against the shape.
2. **Node** (`packages/server-node/src/tools.ts`): add a `case "foo"` to the `callTool` switch and implement the handler method. You do **not** edit `listTools` — it is generated from the contract. Run `npm run build` (this also stages the contract and version into `build/`).
3. **Python** (`packages/server-python/src/opcua_mcp_server/server.py`): add a function decorated with `@mcp.tool(description=_DESC["foo"])`, with typed args (`MCPServer` derives the input schema from them — keep it matching the contract) and `ctx: Context`. For a capability-gated tool, register it conditionally like `read_history_opcua_node`.
4. **Test**: add an end-to-end test in `tests/e2e/test_mcp_e2e.py` (it runs against both servers). The contract-parity test will automatically check that both servers advertise the new tool with the contract's description and parameters; if the tool declares a `resultShape`, assert the returned records against it with `assert_matches_result_shape`.
5. **Document it** in `docs/examples.md` (the central per-tool reference).

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

## Releasing

See **[docs/releasing.md](docs/releasing.md)**. Releases are tag-triggered and
gated on the full suite plus the artifact smoke tests. Two workflows run off the
tag: `publish.yml` ships to npm and PyPI, and `release.yml` builds the `.mcpb`
bundle and the per-platform executables and attaches them to the GitHub release.

Version lives in the package manifests and, for the bundle, in
`packages/server-node/mcpb/manifest.json`;
`tests/unit/test_version_manifests.py` fails if they drift apart.

## Security note

The servers **default** to `SecurityPolicy.None` / `MessageSecurityMode.None`
for local development against the mock. Do **not** use that default against
production OPC UA systems — configure `OPCUA_SECURITY_POLICY`,
`OPCUA_CLIENT_CERT`/`OPCUA_CLIENT_KEY` and credentials
([Configuration](README.md#configuration)), and note the gaps listed in
[SECURITY.md](SECURITY.md) (notably that the server certificate is not pinned,
and that node IDs and written values are not validated beyond what the OPC UA
server enforces).

Both runtimes parse those variables in one place — `src/security.ts` and
`src/opcua_mcp_server/security.py` — with the same defaults and the same error
wording. A change to one belongs in the other, and the unit suites on both sides
assert the shared messages. Parity extends past the parsing: the two must also
reach a server as the same identity, which is why the Python side derives the
session's ApplicationUri from the client certificate as node-opcua does. How
users make that certificate is [docs/certificates.md](docs/certificates.md).
