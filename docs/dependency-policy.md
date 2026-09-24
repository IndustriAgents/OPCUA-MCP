# Dependency support policy

Both servers are executables that sit on a security boundary: an AI agent on one
side, an OPC UA control system on the other. What they do at that boundary —
which certificate they trust, how they parse a chunked message, which MCP
request they accept — is decided largely by their dependencies. So which
versions of those dependencies a user can end up running is a security
question, not only a packaging one.

The lockfiles (`uv.lock`, `packages/server-node/package-lock.json`) make CI and
development repeatable. They do **not** reach users: `pip install
opcua-mcp-server` and `npm install -g opcua-mcp-server` resolve from the ranges
the manifests declare, and pick whatever is newest inside them on the day. This
document says what those ranges are, how each end of them is tested, and how
fast we respond when a dependency changes under us.

The maintainer decision in [#143](https://github.com/IndustriAgents/OPCUA-MCP/issues/143)
is that both runtimes are first-class, so everything here applies to the Python
and the Node package equally.

## Supported runtimes

| Runtime | Manifest declares | Tested in CI | Supported |
|---|---|---|---|
| Python | `requires-python = ">=3.10"` | 3.10 and 3.13 on every PR; 3.10 with the lowest dependency set, 3.13 with the latest | 3.10 – 3.13 |
| Node.js | `"engines": { "node": ">=22.13.0" }` | newest 22.x and 24.x on every PR; exactly 22.13.0 with the lowest dependency set | 22.13+ and 24 |

Newer runtimes (Python 3.14, Node 26) are not blocked by the manifests — an
upper bound on `requires-python` or `engines` breaks installers rather than
protecting anyone — but they are **not supported** until they are in the CI
matrix. Adding one is a PR to `ci.yml` (and to the ruleset's required checks,
in the order the notes in `ci.yml` describe).

A runtime version is dropped in the first minor release after it reaches
upstream end of life, by raising the floor in the manifest and removing it from
CI in the same PR. Next up: **Python 3.10 reaches end of life in October 2026**;
Node 22 in April 2027.

## Direct runtime dependencies

Every direct runtime dependency of both published packages, with the range each
manifest declares. `tests/unit/test_dependency_ranges.py` fails if this table and
the manifests disagree, and if any runtime range loses its floor or its ceiling.

### `opcua-mcp-server` on PyPI — `packages/server-python/pyproject.toml`

| Package | Range | What it does here | Security-sensitive |
|---|---|---|---|
| `cryptography` | `>=50.0.1,<51` | X.509 parsing for the ApplicationUri; python-opcua's channel signing and encryption | Certificates |
| `mcp[cli]` | `>=2.2.0,<3` | MCP protocol, stdio transport, tool input-schema validation | MCP protocol, schema validation |
| `opcua` | `>=0.98.13,<0.99` | OPC UA client stack (python-opcua) | OPC UA transport, certificates |

### `opcua-mcp-server` on npm — `packages/server-node/package.json`

| Package | Range | What it does here | Security-sensitive |
|---|---|---|---|
| `@modelcontextprotocol/sdk` | `^1.26.0` | MCP protocol, stdio transport, request validation | MCP protocol, schema validation |
| `node-opcua-client` | `^2.184.8` | OPC UA client stack | OPC UA transport, certificates |
| `node-opcua-crypto` | `^6.0.0` | Certificate and key loading, X.509 user-identity signing | Certificates |

Every runtime dependency is security-sensitive under the definition below; that
is a property of what this project is, not an accident of the list.

Floor notes:

- `@modelcontextprotocol/sdk` was `^1.0.4`. The Node unit tests and the core
  e2e files do pass on 1.0.4, but every release before 1.26.0 carries a
  published high-severity advisory (GHSA-w48q-cv73-mx4w, GHSA-8r9q-7v3j-jr4g,
  GHSA-345p-7cg4-v4c7). They concern
  HTTP transports and resource templates, which this server does not use — but a
  scanner cannot tell that, and a reachability argument is one refactor away
  from being wrong. The floor is the first release clear of all three.
- `opcua` has one release in range and no successor: python-opcua is
  unmaintained, and its open advisory (CVE-2022-25304) is mitigated in this
  project as [SECURITY.md](../SECURITY.md#known-advisories-in-dependencies)
  describes. Moving off it is [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144).
- `httpx` used to be declared by the Python package. Nothing imported it — the
  MCP SDK moved to `httpx2` — so it is no longer a dependency at all.

### Range rules

- **Python**: `>=floor,<next-major`. For a `0.x` package the minor is the major
  (`opcua>=0.98.13,<0.99`), as semver treats it.
- **Node**: `^floor` (or `~floor`). A caret range is bounded at the next major,
  and on a `0.x` version at the next minor. No `*`, `>=`, `x`, `||`, dist-tags or
  URLs: none of those has a floor to test.
- **The floor is a version CI tests.** The lowest-dependency job installs
  exactly it. A floor is raised when the code needs something newer, or when a
  published advisory affects a version inside the range — whether or not our
  code reaches the vulnerable path.
- **The ceiling moves only by a PR that runs the full suite on the new major.**
  Dependabot opens that PR; see [Updates](#updates).
- Transitive dependencies are not constrained for users (npm does not publish
  the lockfile, and a wheel carries none). The latest-compatible job is what
  tests the set a fresh install resolves.

Build and test tooling (`devDependencies`, the `tests` workspace member, the root
`dev` and `packaging` groups) never reaches a user's install. It is locked, and
floors must still resolve, but it has no ceiling requirement. The mock OPC UA
servers under `packages/` are test fixtures and are not published.

## What is tested, and when

| Dependency set | What it is | Where | When |
|---|---|---|---|
| Locked | Exactly the lockfiles | `ci.yml`: lint and unit tests, the E2E matrix, artifact smoke tests, Windows and macOS | Every PR to `main` and every push to it |
| Lowest supported | Every direct dependency at its floor: uv `--resolution lowest-direct`, and `pin-dependency-floors.mjs` for npm | `dependency-matrix.yml`, job `lowest`, on Python 3.10 and Node 22.13.0 | Weekly (Monday), on any PR that touches a manifest or lockfile, and on demand |
| Latest compatible | No lockfile: the newest version every range allows, direct and transitive | `dependency-matrix.yml`, job `latest`, on Python 3.13 and Node 24 | Same |
| New majors | A range widened to the next major | The Dependabot PR that proposes it, through the whole of `ci.yml` | Weekly |

The artifact smoke tests also install from ranges rather than the lockfiles —
they build the wheel and the npm tarball and install them in isolation — so the
release gate sees a fresh install as of the moment it runs.

Reproducing the two scheduled sets locally:

```bash
# Python, lowest direct dependencies (rewrites uv.lock — restore it afterwards)
uv sync --all-packages --resolution lowest-direct --python 3.10
# Python, latest compatible
uv sync --all-packages --upgrade

# Node, lowest (edits package.json in place — restore it afterwards)
cd packages/server-node
node scripts/pin-dependency-floors.mjs && npm install && npm ls --omit=dev --depth=0
# Node, latest compatible
rm package-lock.json && npm install
```

The `dependency-matrix.yml` jobs are not required status checks: they run only
on a schedule and on dependency PRs, and requiring them would block every PR
that does not touch a manifest.

## Security-sensitive changes

A dependency change is security-sensitive if it touches any of:

- **OPC UA transport** — python-opcua, node-opcua and its `node-opcua-*` packages.
- **Certificates and cryptography** — `cryptography`, `node-opcua-crypto`, and
  anything they load keys or certificates through.
- **MCP protocol** — `mcp`, `@modelcontextprotocol/sdk`, and the transport layer
  under them (`anyio`).
- **Schema validation** — what validates tool arguments before a handler runs:
  pydantic and jsonschema under the Python SDK, the validator bundled with the
  TypeScript SDK.
- **Packaging** — what builds the artifacts users download: hatchling,
  PyInstaller, esbuild, postject, `@anthropic-ai/mcpb`, TypeScript.

That covers every runtime dependency, direct or transitive, and several build
tools. A security-sensitive change needs the **full conformance suite** — the
whole `ci.yml` E2E matrix on both runtimes, plus the artifact smoke tests for
packaging — never unit tests alone. On a PR to `main` that is automatic: those
jobs are required status checks, so nothing merges without them. What it rules
out is merging such a change any other way — a stacked PR, whose base is not
`main`, reports no checks at all until it is retargeted — and it means a lockfile
bump that moves one of these transitively is held to the same bar as a direct one.

Its CHANGELOG entry names the package and both versions.

## Response times

Targets, from when the advisory, failure or announcement becomes public or is
reported to us:

| Event | Triage | Resolution |
|---|---|---|
| Critical or high advisory in a runtime dependency | 2 working days | Release within 7 days: floor raised past the vulnerable versions, or a documented mitigation |
| Moderate advisory | 7 days | Next release, within 30 days |
| Low advisory | 30 days | Next release |
| Advisory with no upstream fix | as above | Mitigation, plus an entry in [SECURITY.md](../SECURITY.md#known-advisories-in-dependencies) saying what is and is not covered |
| New major of a runtime dependency | 30 days | Ceiling widened, or the reason it is not recorded on the Dependabot PR. 7 days if the old major has stopped receiving security fixes |
| `dependency-matrix.yml` goes red | 7 days | Fixed; floor raised; or the broken version excluded (`!=`) with an issue to remove the exclusion. Never by deleting or skipping the job |
| A dependency announces end of life | 30 days | An issue with a migration plan. python-opcua is the standing case: [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144) |
| A runtime reaches end of life | — | Dropped in the next minor release |

A security fix ships in a new release of **both** packages, as every release does.

## Deprecations

A deprecation warning is a dated notice that something will stop working. Left
to scroll past it stops being read — python-opcua alone prints one per OPC UA
message on Python 3.12+ — and a new one arrives unnoticed under the volume. So:

- **Project-owned deprecations fail CI.** Python: every `DeprecationWarning` and
  `PendingDeprecationWarning` raised in the test process is an error
  (`tests/deprecations.py`, loaded from `tests/conftest.py`). Node: `npm test`
  runs with `--throw-deprecation`, which the test runner passes to every test
  file's process. Both servers' stderr: `tests/e2e/test_deprecations_e2e.py`
  starts each runtime with deprecation reporting fully on, drives an unsecured
  and an X.509-authenticated session, and fails on any line reporting a
  deprecation — which is how node-opcua's `endpoint_must_exist` notice, logged
  through its own logger rather than as a process warning, would have been caught.
- **Upstream warnings are allowlisted one by one** in
  [`tests/fixtures/deprecation-allowlist.json`](../tests/fixtures/deprecation-allowlist.json).
  Each entry names the upstream package, the issue that tracks it, an owner, and
  the condition under which it is removed; `tests/unit/test_deprecation_allowlist.py`
  rejects an entry missing any of them. Only a warning raised *inside* a
  dependency qualifies: one attributed to our own code is ours to fix.

Current allowlist: python-opcua's `datetime.utcnow()` / `utcfromtimestamp()`
warnings on Python 3.12+, tracked by #144 and removed when the Python runtime
stops depending on python-opcua. No upstream release will fix them.

Node's `--throw-deprecation` has no per-warning exception. If an upstream
package starts emitting a Node runtime deprecation in the unit tests, the
choice is to fix it upstream, move to a version that does not, or replace the
flag with a `process.on("warning")` filter that reads the same allowlist — not
to drop the flag.

## Updates

[`.github/dependabot.yml`](../.github/dependabot.yml) implements this policy:

- **Weekly** for npm and uv, monthly for GitHub Actions.
- **Runtime dependencies are never grouped.** Each is its own PR, so each gets
  its own review, its own full suite run and its own revert.
- **Tooling is grouped** — minor and patch only — into one weekly PR per
  ecosystem. Tooling majors arrive individually.
- **npm uses `increase-if-necessary`.** A release inside the declared range
  moves only the lockfile, so the floor stays where the lowest job keeps testing
  it; a release outside it (a new major) rewrites the range, and that PR *is* the
  new-major evaluation.
- **Nothing auto-merges.** The merge gate is the required checks — the full E2E
  matrix and the smoke tests — so no update, security-sensitive or not, merges
  on unit tests alone.

A Dependabot PR that has been pushed to by hand stops being rebased by
Dependabot; `@dependabot recreate` restores that.

## Not covered here

- **SBOMs and provenance.** A release's dependency inventory — every resolved
  version in every artifact — belongs with artifact signing and checksums, in
  [#145](https://github.com/IndustriAgents/OPCUA-MCP/issues/145).
- **Real-vendor OPC UA interoperability**, which no dependency range can
  establish: [#147](https://github.com/IndustriAgents/OPCUA-MCP/issues/147) and
  [compatibility.md](compatibility.md).
