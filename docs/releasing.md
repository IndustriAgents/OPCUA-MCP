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
4. Optional, and not done yet: the Apple and Windows code-signing secrets —
   see [Platform signing](#platform-signing--secrets-to-add). Releases work
   without them and say in their notes that the executables are unsigned.

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
tests, and only publishes if they pass (run by hand from a branch, it verifies
and stops — the publish jobs need a tag); `release.yml` builds and attaches nothing
until the same suite has passed on the tag. Both run it in required mode
(`OPCUA_TESTS_REQUIRED=1`), so a missing mock or toolchain fails the release
instead of quietly skipping a subsystem — see
[../tests/README.md](../tests/README.md#required-mode).

Before tagging, check that the latest
[Dependency matrix](https://github.com/IndustriAgents/OPCUA-MCP/actions/workflows/dependency-matrix.yml)
run on `main` is green, or dispatch one. It is the only run that installs the
declared floors and the newest versions the ranges allow, which are what users
of the release will actually resolve — see
[dependency-policy.md](dependency-policy.md).

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
cross-compiled — the workflow builds them on Linux, macOS and Windows runners.
It also attaches the npm tarball, wheel and sdist, so an offline install can come
from the release page.

It is a separate workflow on purpose: a macOS runner being unavailable must not
be able to hold up an npm or PyPI publish. `workflow_dispatch` runs everything up
to and including platform signing without a tag and publishes nothing, which is
the way to test a change to the build or signing scripts.

### What a release carries, and how it is checked (#145)

Every asset is exercised over MCP *as shipped* (after signing, which rewrites
an executable), and every asset gets a CycloneDX SBOM from
[`scripts/sbom.py`](../scripts/sbom.py). Then, for a tag only, and in jobs that
run nothing but pinned actions, `gh`, `cosign` and coreutils:

1. **`SHA256SUMS`** over every asset, SBOMs included, signed keyless with
   Sigstore cosign (`SHA256SUMS.sigstore.json`) — the certificate names
   `release.yml` at the tag.
2. **Build-provenance attestations** (SLSA, `actions/attest-build-provenance`)
   for every asset and for `SHA256SUMS`, and an **SBOM attestation**
   (`actions/attest-sbom`) binding each SBOM to its artifact's digest.
3. **A draft release** is created and uploaded to; the workflow downloads it
   back, checks the file list against the manifest, `sha256sum --check`s it,
   verifies the cosign signature and every attestation with
   `gh attestation verify`, and only then publishes it. A failure leaves an
   unpublished draft, which the next run deletes and recreates.

The release notes gain a *Verifying this release* section and one line per
platform saying whether its executables carry a platform signature — derived
from what the build job actually checked, not from configuration. The
consumer-side commands are in [install.md](install.md#verifying-a-download).

Re-runs are safe: if the tag's release is already public with its `SHA256SUMS`,
the workflow stops before attesting anything (a rebuild's executables would not
match the published ones byte for byte). A public release *without* a manifest —
0.5.1 and earlier — is refused rather than altered; turn it back into a draft
first if it really should be re-attached.

**Recommended repository setting:** enable *immutable releases* (Settings →
General → Releases). The draft-then-publish flow above is what it expects;
once on, a published release's assets and tag cannot be changed, which is what
makes `SHA256SUMS` final.

### Platform signing — secrets to add

Neither certificate exists yet, so today macOS executables are ad-hoc signed and
Windows executables are unsigned, and the release notes say so. The signing steps
are fully wired and switch on by themselves once these **repository secrets**
exist; configuring only part of a set fails the release rather than shipping it
half-signed. The build and test steps never see them — only the signing step
does.

**macOS — Developer ID signature, hardened runtime, notarization**
(requires an [Apple Developer Program](https://developer.apple.com/programs/)
membership):

| Secret | Value |
|---|---|
| `APPLE_CERT_P12` | A *Developer ID Application* certificate and its private key, exported from Keychain Access as `.p12`, base64-encoded (`base64 -i cert.p12`). If codesign cannot build the chain on the runner, re-export with the *Developer ID Certification Authority* intermediate included |
| `APPLE_CERT_PASSWORD` | The password the `.p12` was exported with |
| `APPLE_NOTARY_KEY` | An App Store Connect API key (`AuthKey_XXXX.p8`, role *Developer*) — the file's contents, as-is |
| `APPLE_NOTARY_KEY_ID` | That key's ID |
| `APPLE_NOTARY_ISSUER` | The issuer ID shown above the keys list in App Store Connect → Users and Access → Integrations |

Each executable is signed with `codesign --options runtime --timestamp` and the
runtime's own entitlements
([Node](../packages/server-node/scripts/macos-entitlements.plist): JIT;
[Python](../packages/server-python/packaging/macos-entitlements.plist): unsigned
libraries unpacked at run time — without them each binary is killed on launch,
which the post-signing smoke test would catch), then submitted to `notarytool`
and required to come back *Accepted*. Bare executables cannot be stapled, so
Gatekeeper fetches the ticket online at first launch.

**Windows — Authenticode** (code-signing keys have had to live on an HSM since
2023, so this uses [AzureSignTool](https://github.com/vcsjones/AzureSignTool)
against a certificate whose key was generated in Azure Key Vault Premium, HSM
protected):

| Secret | Value |
|---|---|
| `AZURE_KEY_VAULT_URI` | e.g. `https://opcua-mcp-signing.vault.azure.net` |
| `AZURE_CERT_NAME` | The certificate's name in that vault |
| `AZURE_TENANT_ID` | Tenant of the app registration below |
| `AZURE_CLIENT_ID` | An app registration (service principal) with the *Key Vault Crypto User* and *Key Vault Certificate User* roles on the vault |
| `AZURE_CLIENT_SECRET` | A client secret for that app registration |

Signatures are RFC 3161 timestamped (DigiCert), so they stay valid after the
certificate expires. The build checks every file with `Get-AuthenticodeSignature`
before continuing.

### Keeping the pins current

Every third-party action is pinned to a commit SHA with the tag in a comment;
Dependabot's monthly `github-actions` PR moves both, across the three workflows
and the conformance action. Two versions live in `release.yml` itself and are
not Dependabot's to update: the AzureSignTool download (version and SHA-256,
taken from the release's asset digests) and, implicitly, the cosign release
`sigstore/cosign-installer` installs by default.

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
