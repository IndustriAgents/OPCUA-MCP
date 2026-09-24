## Summary

<!-- What does this PR do and why? -->

## Type of change

- [ ] Bug fix
- [ ] New feature / new tool
- [ ] Documentation
- [ ] Refactor / chore

## Affected implementation(s)

- [ ] Python (`opcua-mcp-server`)
- [ ] Node / TypeScript (`opcua-mcp-server`)
- [ ] Tests / docs / CI only

## Checklist

- [ ] If I added or changed a tool, I updated **both** servers (or explained why not).
- [ ] I updated the README / docs (examples, testing) where relevant.
- [ ] If I changed `contract/tools.json`, `contract/config.json` or the version, I ran `npm run config:generate` in `packages/server-node` and committed the generated diff.
- [ ] Release PR, or a change to authentication, authorisation, audit or a default: I did the [security-doc review](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/releasing.md#security-doc-review) and say so above.
- [ ] I ran the end-to-end suite locally (`uv sync --all-packages`, then `cd tests && uv run --no-sync pytest -v`).
- [ ] I added or updated tests covering the change.
- [ ] I updated `CHANGELOG.md` under **Unreleased**.

## How to test

<!-- Commands, tool calls, and expected output a reviewer can run. -->
