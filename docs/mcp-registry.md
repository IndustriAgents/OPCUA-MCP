# MCP Registry listing

The root [`server.json`](../server.json) describes this server for the official
[MCP Registry](https://modelcontextprotocol.io/registry): the npm package, its
stdio transport, and every environment variable it reads. That last part —
`packages[].environmentVariables` — is generated from
[`contract/config.json`](../contract/config.json); edit the schema and run
`npm run config:generate` in `packages/server-node` rather than editing it here.
The registry checks the published npm package for
`"mcpName": "io.github.midhunxavier/opcua"`. GitHub authentication separately
proves permission to publish in that namespace. The repository is now under
`IndustriAgents`, but the published package still uses the existing
`io.github.midhunxavier` namespace.

> **Status: ready to publish.** npm serves version 0.6.0 with the matching
> `mcpName`. The first submission requires GitHub device authorization as
> `midhunxavier`; no new npm release is needed.

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
mcp-publisher validate server.json
mcp-publisher login github     # authorize as midhunxavier at the GitHub device prompt
mcp-publisher publish server.json
```

Verify the entry resolves, and that it names this repository and the exact
version published:

```bash
VERSION=$(node -p "require('./server.json').version")
curl --fail --silent --show-error \
  "https://registry.modelcontextprotocol.io/v0.1/servers/io.github.midhunxavier%2Fopcua/versions/$VERSION" \
  | jq -e --arg version "$VERSION" '
      .server
      | .name == "io.github.midhunxavier/opcua"
        and .version == $version
        and .repository.url == "https://github.com/IndustriAgents/OPCUA-MCP"
        and any(.packages[]; .registryType == "npm" and .identifier == "opcua-mcp-server"
            and .version == $version and .transport.type == "stdio")
    '
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

`server.json` declares its schema. Run `mcp-publisher validate server.json`
after any edit to check it against the official Registry.

The registry is still in preview, so re-read the quickstart before the first
submission — the schema date in `$schema` is pinned here on purpose, and a newer
one may exist. Automating publication in `publish.yml` is worth doing only after
one manual submission has succeeded.

## Glama and awesome-mcp-servers

[Add the GitHub repository on Glama](https://glama.ai/mcp/servers) once, using
`https://github.com/IndustriAgents/OPCUA-MCP`. After sign-in, claim the listing
as a maintainer listed in the root [`glama.json`](../glama.json). Follow Glama's
[build and release instructions](https://glama.ai/blog/2026-03-15-how-to-make-a-release)
and use the current published npm version in the Dockerfile:

```dockerfile
FROM node:22-bookworm-slim
RUN npm install --global --ignore-scripts --no-audit --no-fund opcua-mcp-server@0.6.0
USER node
ENV OPCUA_PROFILE=observe \
    OPCUA_SERVER_URL=opc.tcp://127.0.0.1:4840
CMD ["opcua-mcp-server"]
```

The server responds to MCP initialization and tool discovery without a live
OPC UA endpoint. The loopback URL lets Glama run those checks without plant
access. Once the listing passes its checks and the score badge works, submit
one entry under **Industrial & IoT** in
[awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers),
following its [contribution rules](https://github.com/punkpeye/awesome-mcp-servers/blob/main/CONTRIBUTING.md).
