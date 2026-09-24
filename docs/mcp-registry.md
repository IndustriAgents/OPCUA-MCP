# MCP Registry listing

The root [`server.json`](../server.json) describes this server for the official
[MCP Registry](https://modelcontextprotocol.io/registry): the npm package, its
stdio transport, and every environment variable it reads. That last part —
`packages[].environmentVariables` — is generated from
[`contract/config.json`](../contract/config.json); edit the schema and run
`npm run config:generate` in `packages/server-node` rather than editing it here.
The npm manifest carries the matching `"mcpName": "io.github.midhunxavier/opcua"`, which is how
the registry proves the package and this repository belong to the same owner.

> **Status: metadata only.** Nothing here publishes a listing, and no listing has
> been submitted yet. `server.json` carries the release version, stamped from the
> npm manifest by the same generator; every published npm version from 0.4.1 on
> carries `mcpName`, so the published release is the one to submit.

## Before submitting

1. Cut a release as usual ([releasing.md](releasing.md)). Setting the version in
   `packages/server-node/package.json` and running `npm run config:generate`
   stamps it into both `version` fields of `server.json`; `npm run config:check`
   and the unit tests fail if they drift from the npm manifest.
2. Wait for the tag-triggered publish to finish. The registry reads npm, not
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

- **`OPCUA_SERVER_URL` and `OPCUA_PROFILE` are the only required variables**,
  and both carry defaults: the mock's endpoint and `observe`. No endpoint of
  anyone's is embedded, and no credential is.
- **`OPCUA_PASSWORD` is marked `isSecret`**, as are the paths to the two private
  keys (`OPCUA_CLIENT_KEY`, `OPCUA_USER_KEY`), so a client that honours the flag
  will not store or display them in the clear.
- **Every variable either runtime reads is listed** — including server
  certificate pinning, X.509 user authentication, value-range overrides and the
  audit file — because the list is generated from the same schema the unit
  suite holds both runtimes to.
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
