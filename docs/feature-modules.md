# Feature module boundaries

The migration in [#141](https://github.com/IndustriAgents/OPCUA-MCP/issues/141)
extracts one feature at a time. **Only current-value reads have moved so far.**
Other tools still use the existing execution and feature code.

| Layer | Python | Node | Ownership |
|---|---|---|---|
| MCP adapter | `server.py:read_opcua_nodes` | `tools.ts:readOpcuaNodes` | Context lookup, injected port construction, protocol result/error conversion |
| Read use case and port | `application/read.py` | `application/read.ts` | Ordered logical reads, sequential service batches, one metadata lookup, JSON records |
| Native read adapter | `adapters/opcua_read.py` | `adapters/opcua-read.ts` | Native node/attribute/variant access, codec, typed failure with its original cause |

A port is bound to the session selected by the existing execution pipeline for
one call. The metadata cache stays on the server instance and is still cleared
when that session changes. Application modules import neither MCP nor the native
OPC UA library. They can be tested with an in-memory port; no subscription,
connection or process-global feature state is created by the read use case.

Python's native adapter runs blocking calls on worker threads behind an async
port. Node awaits its library's async services. Both issue each batch in order,
then ask for metadata for the complete logical read. `AdapterFailure` retains
its cause so the shared recovery classifier still sees timeouts and broken
connections. The MCP adapter translates anticipated failures into the existing
contract error frame. Control retry and uncertain-outcome rules are unchanged.

`tests/fixtures/read-port.json` characterizes batch order, mixed statuses,
Unicode and metadata. Both runtime suites execute it through a fake port;
native status/codec fixtures and the existing differential/E2E suite continue
to check the adapter. Import and file-size checks in
`tests/unit/test_feature_boundaries.py` enforce the new application and adapter
boundaries. As subsequent features move, these checks apply to their modules.

The remaining slices are browse, write, methods, history, events, alarms,
subscriptions and diagnostics, followed by extraction of the common execution
pipeline and protocol-independent typed errors. This document does not claim
that the central modules already meet #141's final size or dependency limits.
