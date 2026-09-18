---
name: Compatibility report
about: Report how the servers behaved against a real OPC UA server
title: "[Compatibility] <server product and version>"
labels: ""
assignees: ""
---

Results from a real OPC UA server are the most useful thing you can send. Only
test on equipment you are authorised to use, and keep writes, method calls and
alarm acknowledgements to a simulator or an isolated lab.

**Do not paste credentials, private keys, internal hostnames or production
process data.** Sanitise transcripts before posting.

## Environment

- OPC UA server product and exact version:
- MCP server version (`opcua-mcp-server --version` or commit):
- Which implementation?
  - [ ] Python
  - [ ] Node
- Runtime version (`python --version` / `node --version`):
- MCP client and version:
- OS:
- Date tested:

## Connection

- `OPCUA_SECURITY_POLICY` / `OPCUA_SECURITY_MODE`:
- Identity: anonymous / username
- `OPCUA_PROFILE` (`observe`, `operator`, `full`):
- Permissions of the OPC UA account, as far as you know them:

## Results

Mark each one **PASS**, **FAIL**, **UNSUPPORTED** (the server does not offer it)
or **NOT TESTED**. Leave the notes column empty unless something is worth saying.

| Operation | Result | Notes |
|---|---|---|
| `read_opcua_node` / `read_multiple_opcua_nodes` | | |
| `browse_opcua_node_children` / `get_all_variables` | | |
| `write_opcua_node` / `write_multiple_opcua_nodes` (lab only) | | |
| `call_opcua_method` (lab only) | | |
| `subscribe_opcua_node` / `list_subscriptions` / `unsubscribe_opcua_node` | | |
| `read_history_opcua_node` | | |
| `read_aggregate_opcua_node` | | |
| `subscribe_events` / `read_events` | | |
| `list_active_alarms` | | |
| `acknowledge_alarm` (lab only) | | |

## What went wrong, if anything

Error messages, the tool call that produced them, and anything the server
refused. A sanitised config with secrets removed helps a lot.
