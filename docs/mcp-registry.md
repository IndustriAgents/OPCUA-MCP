# Prepare the official MCP Registry listing

The root [server.json](../server.json) describes the npm distribution over stdio.
The package manifest carries the matching
`mcpName: io.github.midhunxavier/opcua`.

**Status: metadata prepared; not published by this change.** The versions
currently match the source manifests (0.3.0). The already-published 0.3.0 package
predates this verification field and cannot be repaired by editing GitHub.
Use a new package version for the first registry publication.

## Before the next package release

1. Follow [releasing.md](releasing.md) to bump all four package/bundle manifests,
   refresh the npm lockfile, update the changelog, and pass the release checks.
2. Update `version` and `packages[0].version` in root `server.json` to the
   same new version. Do not reuse an already-published npm version.
3. Confirm `server.json.name` equals `packages/server-node/package.json.mcpName`.
4. Validate `server.json` against its declared JSON schema with a JSON Schema
   validator. Review every environment-variable description.
5. Release the npm package through the existing tag-triggered workflow. Wait for
   the release to succeed before submitting the registry entry.

From the repository root, inspect the metadata that npm actually serves:

```bash
VERSION=$(node -p "require('./server.json').packages[0].version")
npm view "opcua-mcp-server@$VERSION" version mcpName
```

The result must show the selected new version and
`io.github.midhunxavier/opcua`. Stop if npm returns an older version, omits
`mcpName`, or returns a different name.

## Publish the registry entry

Install the official `mcp-publisher` CLI following the
[registry quickstart](https://modelcontextprotocol.io/registry/quickstart).
Then, from the repository root:

```bash
mcp-publisher login github
mcp-publisher publish
```

Authenticate as the GitHub owner authorized for the `io.github.midhunxavier/`
namespace. Publication is a separate launch step; adding these files does not
create a listing or change the released package.

Verify the returned entry names this repository and the exact published package:

```bash
curl --fail --get 'https://registry.modelcontextprotocol.io/v0.1/servers' \
  --data-urlencode 'search=io.github.midhunxavier/opcua'
```

Only add a registry badge or announce the listing after it is discoverable.

## Configuration choices

- The endpoint is required; no production endpoint or credentials are embedded.
- The password field is marked secret.
- Security fields are optional because local mocks use unsecured connections.
  Their descriptions explain when certificates and encryption are needed.
- Node 22.13+ is required. No HTTP endpoint or hosted service is advertised.
- The first metadata entry covers npm. PyPI remains available through the
  documented `uvx` setup; adding it to the registry requires its own package
  ownership-verification preparation.
- This metadata does not add read-only enforcement, certificate pinning, or an
  approval mechanism. The existing [security limitations](../SECURITY.md) apply.

The official registry is in preview; check the linked publication guide again
before launch. Automated registry publication can be added after the first
verified manual publication.
