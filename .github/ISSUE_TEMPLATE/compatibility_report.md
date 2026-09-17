---
name: Compatibility report
about: Report a tested OPC UA server and MCP client combination
title: "[Compatibility] Server name and version"
labels: ""
assignees: ""
---

## Environment

- OPC UA server product and exact version:
- OPC UA MCP package version or commit:
- Runtime and version (Python / Node):
- MCP client and version:
- OS:
- Test date:
- Simulator / lab / other authorized environment:

## Connection

- Security policy and mode:
- Anonymous or username identity:
- Account permissions relevant to the tested operations:

Do not include credentials, private keys, private endpoints, or production data.

## Results

Use PASS, FAIL, UNSUPPORTED, or NOT TESTED. Include exact tool names.

| Operation | Result | Evidence / limitation |
|---|---|---|
| Read | | |
| Browse | | |
| Batch read | | |
| Write / batch write (authorized lab only) | | |
| Method call (authorized lab only) | | |
| Data subscription / cancellation | | |
| Raw history | | |
| Server-side aggregate | | |
| Event subscription / reading | | |
| Retained alarms | | |
| Alarm acknowledgement (authorized lab only) | | |

## Reproduction and evidence

Provide a minimal configuration with secrets removed, prompts/tool calls,
sanitized results, and any unsupported server capabilities.
