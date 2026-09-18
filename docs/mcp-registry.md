# MCP Registry listing

The root [`server.json`](../server.json) describes this server for the official
[MCP Registry](https://modelcontextprotocol.io/registry): the npm package, its
stdio transport, and every environment variable it reads. The npm manifest
carries the matching `"mcpName": "io.github.midhunxavier/opcua"`, which is how
the registry proves the package and this repository belong to the same owner.

> **Status: metadata only.** Nothing here publishes a listing. `server.json` is
> at 0.3.0 to match the manifests, but the already-published `opcua-mcp-server@0.3.0`
> on npm predates `mcpName`, and an npm version is immutable — editing this
> repository cannot add the field to it. **The first registry submission needs a
> new npm release.**

## Before submitting

1. Cut a release as usual ([releasing.md](releasing.md)): bump all four
   manifests, refresh the npm lockfile, move the `[Unreleased]` changelog
   entries, and tag.
2. Set `version` and `packages[0].version` in `server.json` to that same new
   version. The unit tests fail if they drift from the npm manifest.
3. Wait for the tag-triggered publish to finish. The registry reads npm, not
   this repository, so submitting first simply fails.

Then check what npm actually serves, from the repository root:

```bash
VERSION=$(node -p "require('./server.json').packages[0].version")
npm view "opcua-mcp-server@$VERSION" version mcpName
```

It must print that version and `io.github.midhunxavier/opcua`. If `mcpName` is
missing, the published tarball was built before the field landed — publish
another version rather than trying to patch the listing.

## Submitting

Install the official `mcp-publisher` CLI as described in the
[registry quickstart](https://modelcontextprotocol.io/registry/quickstart), then
from the repository root:

```bash
mcp-publisher login github     # as the owner of the io.github.midhunxavier namespace
mcp-publisher publish
```

Verify the entry resolves, and that it names this repository and the exact
version published:

```bash
curl --fail --get 'https://registry.modelcontextprotocol.io/v0.1/servers' \
  --data-urlencode 'search=io.github.midhunxavier/opcua'
```

Only link to the listing once that returns it.

## What the metadata says, and does not

- **`OPCUA_SERVER_URL` is the only required variable.** No endpoint of anyone's
  is embedded, and no credential is.
- **`OPCUA_PASSWORD` is marked `isSecret`**, so a client that honours the flag
  will not store or display it in the clear.
- **Security variables are optional** because the mocks need none. Their
  descriptions say when a certificate and policy are required instead.
- **`OPCUA_PROFILE` defaults to `observe`**, the same default the servers apply.
  A client reading `server.json` sees an observe-only server unless the operator
  widens it deliberately.
- **npm only, for now.** The PyPI distribution stays documented in
  [install.md](install.md); listing it as a second package means preparing its
  own ownership verification, which is not done here.
- **No transport beyond stdio**, and no hosted endpoint, is advertised.

Publishing a listing changes nothing about the server's behaviour or its
security posture: the limitations in [SECURITY.md](../SECURITY.md) apply
unchanged.

## Keeping it valid

`server.json` declares its schema. Validate it after any edit:

```bash
uv run --no-project --with jsonschema python - <<'PY'
import json, urllib.request, jsonschema
inst = json.load(open("server.json"))
schema = json.load(urllib.request.urlopen(inst["$schema"]))
jsonschema.Draft7Validator(schema).validate(inst)
print("server.json is valid")
PY
```

The registry is still in preview, so re-read the quickstart before the first
submission — the schema date in `$schema` is pinned here on purpose, and a newer
one may exist. Automating publication in `publish.yml` is worth doing only after
one manual submission has succeeded.
