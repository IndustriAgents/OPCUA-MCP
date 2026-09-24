---
name: Bug report
about: Report a problem with one of the MCP servers
title: "[Bug] "
labels: bug
assignees: ""
---

**Which implementation?**
- [ ] Python (`opcua-mcp-server`)
- [ ] Node / TypeScript (`opcua-mcp-server`)
- [ ] Both, and they behave **differently** — a runtime divergence. Please paste
      what each runtime returned for the same call. Differences the project has
      declared are listed in
      [docs/compatibility.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/compatibility.md#runtime-differences);
      anything else is a bug, and blocks the next release of both packages
      ([ADR 0001](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/adr/0001-two-first-class-runtimes.md)).
      If one runtime is *less safe* than the other, report it privately
      instead — see SECURITY.md.

**Describe the bug**
A clear and concise description of what the bug is.

**To reproduce**
Steps to reproduce, including the MCP client (Claude Desktop / Claude Code /
Cursor / Inspector), the tool you called, and the arguments.

1.
2.
3.

**Expected behavior**
What you expected to happen.

**Actual behavior / error output**
```
paste any error message or tool output here
```

**Environment**
- OS:
- Python / Node version:
- Package version (npm `opcua-mcp-server` or commit SHA):
- OPC UA server (mock from this repo / vendor + model):

**Additional context**
Anything else that might help — node IDs, server config, logs.
