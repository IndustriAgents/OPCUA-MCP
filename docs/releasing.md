# Releasing

Publishing is automated and tag-triggered. It is deliberately not a command you
run by hand — the npm package needs `npm run build` to stage `build/contract.json`
and `build/version.json`, and a hand-run `npm publish` that skips it ships a
package that dies at startup.

## One-time setup

Neither registry is configured yet; both are needed before the first release.

1. **npm** — create an automation token and add it as the `NPM_TOKEN` repository
   secret. Publishing uses `--provenance`, which needs `id-token: write` (already
   set in the workflow).
2. **PyPI** — add a
   [trusted publisher](https://docs.pypi.org/trusted-publishers/) for this repo,
   workflow `publish.yml`, environment `pypi`. No token to store.
3. Create the `pypi` GitHub environment (optionally with a required reviewer, so
   a publish needs an explicit approval).

## A note on READMEs

Each package's README ships *inside* its artifact — `files: [..., "README.md"]`
for npm, `readme = "README.md"` for the wheel — and registry pages are frozen per
version. So edits to `packages/server-node/README.md` or
`packages/server-python/README.md` appear on GitHub immediately but do not reach
npmjs.com or pypi.org until the next publish, and a published version can never
be corrected. Their tool and configuration tables are generated (step 1 below),
so those at least cannot ship stale; the prose around them still needs reading
before a release.

## Security-doc review

A release whose changes touch authentication, authorisation, the audit trail or
a default — anything in `contract/config.json` with `securityRelevant: true`,
the policy layer, or a `guard` in `contract/tools.json` — gets one more item in
its release PR: re-read [SECURITY.md](../SECURITY.md), the Security sections of
the README (Going to production), `docs/configuration.md` and both package
READMEs, the security notes in
[CONTRIBUTING.md](../CONTRIBUTING.md) and the `long_description` in
`mcpb/manifest.json`, and say in the PR that they still describe what ships.
Stale security documentation gives users the wrong threat model, and none of it
is generated.

## Cutting a release

```bash
# 1. Set the version in its one source, packages/server-node/package.json, and
#    stamp it everywhere else. The generator writes it into mcpb/manifest.json,
#    both version fields of server.json, both pyproject.toml files,
#    package-lock.json, uv.lock and ROADMAP.md, and regenerates the reference
#    docs; `npm run config:check` in CI fails if any copy is left behind.
cd packages/server-node && npm run config:generate && cd ../..

# 2. Move CHANGELOG entries from [Unreleased] into the new version, and add the
#    comparison link at the bottom. Commit the generated diff with the bump, so
#    the release PR shows exactly what the published READMEs will say.

# 3. Verify locally exactly as CI will. The smoke tier builds the .mcpb bundle
#    and both single-file executables, so it needs the packaging group.
cd packages/server-node && npm ci && npm run build && npm test && cd ../..
uv sync --all-packages --group packaging
uv run ruff check . && uv run ruff format --check .
cd tests && OPCUA_TESTS_REQUIRED=1 uv run --no-sync pytest e2e/ unit/ \
  && OPCUA_TESTS_REQUIRED=1 uv run --no-sync pytest -m smoke smoke/

# 4. Commit, then tag. The tag must match the manifests; the workflow checks.
git tag v0.2.0 && git push origin v0.2.0
```

The `publish.yml` workflow then runs the full suite plus the artifact smoke
tests, and only publishes if they pass; `release.yml` builds and attaches nothing
until the same suite has passed on the tag. Both run it in required mode
(`OPCUA_TESTS_REQUIRED=1`), so a missing mock or toolchain fails the release
instead of quietly skipping a subsystem — see
[../tests/README.md](../tests/README.md#required-mode).

## The conformance matrix in the release notes

Every release's notes link the real-server conformance matrix **as of its tag**,
so a reader can see which servers that exact version was run against and what
was found:

```markdown
Conformance: [real-server matrix for v0.6.0](https://github.com/IndustriAgents/OPCUA-MCP/blob/v0.6.0/docs/compatibility.md#real-server-conformance)
```

Put the line in the release's CHANGELOG section and in the GitHub release body.
Link the tag, never `main`: the matrix on `main` moves on, and the tag is what
pins both the matrix and the dated result files it was generated from.

The matrix records the package version each result was produced with. A
release may be called **production-qualified** for a server only when the
matrix at its tag has a result for that server *at the version being released*
and its level is *Supported*, or *Partially supported* with each finding named
in the release notes. To get there, run the harness on the release commit
before tagging — against every lab server and any vendor server you have — and
commit the results with the re-rendered matrix:

```bash
compatibility/labs/open62541/build.sh && compatibility/labs/milo/build.sh
export OPEN62541_LAB_SERVER=.conformance/open62541/lab_server
export MILO_CLASSPATH="$(cat .conformance/milo/classpath)"
export OPCUA_CONFORMANCE_USERNAME=lab OPCUA_CONFORMANCE_PASSWORD="$(openssl rand -hex 16)"
export MILO_EXAMPLE_USERNAME=user MILO_EXAMPLE_PASSWORD=password1   # Milo's compiled-in demo account
cd tests
uv run --no-sync python -m conformance run --config ../compatibility/labs/open62541.json
uv run --no-sync python -m conformance run --config ../compatibility/labs/milo.json
uv run --no-sync python -m conformance render
```

A release without fresh results is still a release; its notes then say which
version the linked results were produced with, and do not claim production
qualification. The unit tier (step 3 above) fails if the matrix does not match
the committed results or a result carries an unclassified failure, so a stale or
hand-edited matrix cannot be tagged. The same harness runs in CI on demand —
**Actions → Real-server conformance** — against both lab servers built from
source, and uploads the results as an artifact rather than committing them.

## The downloadable artifacts

`release.yml` runs off the same tag and handles what `publish.yml` cannot: the
`.mcpb` MCP bundle, and a single-file executable per runtime per platform. Those
executables embed the interpreter they were built with, so they cannot be
cross-compiled — the workflow builds them on Linux, macOS and Windows runners,
checks each one starts and reports the right version, and attaches everything to
the GitHub release (creating it from the tag if it does not exist yet).

It is a separate workflow on purpose: a macOS runner being unavailable must not
be able to hold up an npm or PyPI publish. Uploads use `--clobber`, so re-running
after a partial failure is safe. `workflow_dispatch` builds the artifacts without
cutting a tag, which is the way to test a change to the build scripts.

macOS binaries are ad-hoc signed rather than notarised, and Windows binaries are
unsigned, so first launch needs a Gatekeeper or SmartScreen override. That is
documented in [install.md](install.md); proper signing needs an Apple Developer
account and a Windows code-signing certificate, and is not set up.

## After the first `opcua-mcp-server` release

The old npm name needs a pointer to the new one:

```bash
npm deprecate opcua-mcp-npx-server \
  "Renamed to opcua-mcp-server — https://github.com/IndustriAgents/OPCUA-MCP"
```

**Do not unpublish it.** That breaks existing installs, and npm blocks unpublish
after 72 hours anyway.

## Why the smoke tests gate the release

They build every artifact a user can download — tarball, wheel, `.mcpb` bundle
and both executables — install them somewhere isolated, and drive them over MCP.
Everything else in CI runs from the source tree and from `uv.lock`, so it cannot
see packaging faults, a dependency range that resolves to a breaking major, or a
bundling change that only breaks once `node_modules` is no longer on disk. The
first two have already shipped broken releases here.

## The registry manifest

The root [`server.json`](../server.json) carries the version twice — its own
`version` and `packages[0].version` — and both have to match the npm package,
which is why step 1 stamps them. A unit test fails if they drift, and so does
the `name` / `mcpName` pair that ties the registry entry to the published
package.

`server.json` is not published by any workflow here. Submitting it to the MCP
Registry is a separate manual step, and the first submission needs a release
whose npm tarball carries `mcpName` — see [mcp-registry.md](mcp-registry.md).
