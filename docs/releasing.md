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
npmjs.com or pypi.org until the next publish. Worth checking those two files
before cutting a release.

## Cutting a release

```bash
# 1. Bump all four manifests together — they release as a unit and a test
#    enforces that they match.
#    packages/server-node/package.json
#    packages/server-python/pyproject.toml
#    packages/mock-server/pyproject.toml
#    packages/server-node/mcpb/manifest.json
#
#    Then refresh the npm lockfile, which records the root version twice and
#    will not be updated by editing package.json alone:
#      cd packages/server-node && npm install --package-lock-only

# 2. Move CHANGELOG entries from [Unreleased] into the new version, and add the
#    comparison link at the bottom.

# 3. Verify locally exactly as CI will. The smoke tier builds the .mcpb bundle
#    and both single-file executables, so it needs the packaging group.
cd packages/server-node && npm ci && npm run build && npm test && cd ../..
uv sync --all-packages --group packaging
uv run ruff check . && uv run ruff format --check .
cd tests && uv run --no-sync pytest && uv run --no-sync pytest -m smoke smoke/

# 4. Commit, then tag. The tag must match the manifests; the workflow checks.
git tag v0.2.0 && git push origin v0.2.0
```

The `publish.yml` workflow then runs the full suite plus the artifact smoke
tests, and only publishes if they pass.

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
`version` and `packages[0].version` — and both have to match the npm package, so
they belong in step 1 of the bump above. A unit test fails if they drift, and so
does the `name` / `mcpName` pair that ties the registry entry to the published
package.

`server.json` is not published by any workflow here. Submitting it to the MCP
Registry is a separate manual step, and the first submission needs a release
whose npm tarball carries `mcpName` — see [mcp-registry.md](mcp-registry.md).
