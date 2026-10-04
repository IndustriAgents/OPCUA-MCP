# Feature module boundaries

The migration in [#141](https://github.com/IndustriAgents/OPCUA-MCP/issues/141)
extracts one feature at a time. **Reads, browse, writes, methods, value history, events, alarms, subscriptions and diagnostics have moved so far.**
Every feature uses the shared execution and recovery pipeline; central modules compose instance-owned runtime and protocol adapters.

| Layer | Python | Node | Ownership |
|---|---|---|---|
| Protocol registration/results | `protocol/tools.py`, `protocol/value_tools.py`, `protocol/monitoring_tools.py`, `protocol/results.py` | `protocol/dispatch.ts`, `protocol/results.ts` | Stable tool signatures, contract dispatch, context lookup, output schemas and MCP result conversion |
| MCP adapter | `protocol/value_tools.py`, `protocol/monitoring_tools.py` | `protocol/feature-handlers.ts` | Context lookup, injected port construction, protocol result/error conversion |
| Read use case and port | `application/read.py` | `application/read.ts` | Ordered logical reads, sequential service batches, one metadata lookup, JSON records |
| Browse use case and port | `application/browse.py` | `application/browse.ts` | Path matching, bounded breadth-first traversal, filtering, cycle detection and completeness |
| Native browse adapter | `adapters/opcua_browse.py` | `adapters/opcua-browse.ts` | Browse/continuation services, attribute codecs and best-effort enrichment |
| Write use case and port | `application/write.py` | `application/write.ts` | Whole-batch bounds, inference/current-read ordering, conversion result correlation and one send |
| Native write adapter | `adapters/opcua_write.py` | `adapters/opcua-write.ts` | Native current values, engineering metadata, Variant preparation and the single Write service |
| Invocation and recovery policy | `application/invocation.py` | `application/invocation.ts` | Connected attempt, session identity, one read resend, reauthorization/reaudit/recheck and control uncertainty |
| Execution envelope | `application/execution.py` | `application/execution.ts` | Bounds/schema checks, waiting for in-flight connections, authorization, fail-closed allowed audit, outcome audit and protocol-normalization hooks |
| Diagnostics use case and port | `application/diagnostics.py` | `application/diagnostics.ts` | Fast connecting status, disconnected error records and capabilities sampled after the read |
| Native diagnostics adapter | `adapters/opcua_diagnostics.py` | `adapters/opcua-diagnostics.ts` | Native status/namespace/summary decoding and connection recovery with original causes |
| Subscription use cases and port | `application/subscriptions.py` | `application/subscriptions.ts` | Filter relationships, active caps, whole-batch range/ID checks, ordered creation/cancellation and loss completeness |
| Native subscription adapter | `adapters/opcua_subscriptions.py` | `adapters/opcua-subscriptions.ts` | Existing per-instance manager, engineering metadata and lazy native session/client access |
| Alarm use cases and port | `application/alarms.py` | `application/alarms.ts` | Duration relationships, cached condition selection, caller-specific results, status/error framing and one action |
| Native alarm adapter | `adapters/opcua_alarms.py` | `adapters/opcua-alarms.ts` | Condition refresh, native action methods, status normalization and lazy client/session selection |
| Event use cases and port | `application/events.py` | `application/events.ts` | Applied subscription settings, ordered loss notices, history windows and filtered-page completeness |
| Native event adapter | `adapters/opcua_events.py` | `adapters/opcua-events.ts` | Native event subscription/history calls and normalized per-instance buffer drains |
| History use case and port | `application/history.py` | `application/history.ts` | Date windows, aggregate names, interval bounds and structured completeness |
| Native history adapter | `adapters/opcua_history.py` | `adapters/opcua-history.ts` | Native history requests, page drain/release, status and value codecs |
| Method use case and port | `application/methods.py` | `application/methods.ts` | Match raw arguments to normalized declarations, canonical result identifiers, one call and error framing |
| Native method adapter | `adapters/opcua_methods.py` | `adapters/opcua-methods.ts` | Resolve native datatype ancestry, encode variants, call the service once, normalize native status and outputs |
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

Central `server.py` and `tools.ts`, and all new application, native, protocol and infrastructure modules, are capped at 400 lines by boundary tests.

Browse uses normalized references rather than native NodeIds, names or enums.
The native adapter drains reference continuation points, reads attributes and
enriches type definitions/values. A failed descendant still marks an incomplete
walk; a failed root still fails the request. Filtering does not prune descent,
and skipped Server references still consume the existing traversal budget.
`tests/fixtures/browse-port.json` covers those boundaries, cycles, breadth-first
order, absolute/relative paths and Unicode using a fake tree in both runtimes.

Python translates native failures inside its worker before asyncio transfers the
exception back to the caller; this preserves the original timeout cause even on
Python 3.10. Error description is a pure shared helper in `errors.py`, so the
application no longer imports the connection layer to format a failure.

Method metadata crosses the port as builtin datatype names and array flags,
without native enum or Variant instances. Extra arguments retain the existing
scalar fallback; a missing metadata definition still uses that fallback. An
unsupported declared datatype fails before the method call. Native conversion
and service failures retain their causes, while encoded-size refusals keep
their direct refusal frame. `tests/fixtures/method-port.json` verifies metadata
before invocation, nullable optional arguments, one call on failure and
structured array outputs. The shared control pipeline still owns authorization,
audit and the prohibition on resending a possibly applied action.

The write port is created for one invocation. Its prepared native values and
inference readings never leave that adapter or persist across requests. The
application reads inferred/current values once, checks every engineering and
movement bound before preparing any value, then correlates conversion statuses
to the input order. A hard encoded-size refusal abandons all preparations;
conversion failures keep their per-node status. There is at most one send,
including when that send fails. Shared fake-port cases in `write-port.json`
characterize those boundaries without a connection. Native adapter tests retain
the original timeout cause before Python crosses its worker-thread boundary.

Value history uses normalized records across its port. Native continuation points
remain within one adapter call: raw reads release their point; aggregate reads
drain every bounded native page with the original request details. The application
owns date windows, offered aggregate names, interval refusal and completeness,
including whether a forward read has a resumable timestamp. The clock is
injectable for omitted aggregate end times. `history-port.json` characterizes
those query rules and typed failures; existing native request/page tests retain
the continuation protocol checks. Event history moves with the events slice.

Event application services report applied buffer settings, drain loss notices in
the existing order and count fetched history records before severity filtering.
The events port binds the selected session to the existing instance-owned event
manager; it creates no additional global buffer or subscription state. Native
subscription/history exceptions retain their original causes. Shared cases in
`events-port.json` cover buffer clamping, missing subscriptions, overflow plus a
reconnect gap, empty drains and the omitted one-hour history window with an
injected clock. Alarm actions remain in the next slice.

Alarm ports select a client/session lazily when a service is needed, so invalid
duration relationships and unknown EventIds remain refusals before a service.
Successful refreshes update the existing remembered condition cache; failed
refreshes do not. The application preserves Good subcode names, distinguishes
the dedicated acknowledgement shape from the action shape, and performs one
native action. Shared `alarms-port.json` cases characterize those rules and
failure framing; native timeout tests retain the original cause across the
Python worker boundary. The separate #157 alias fix is applied when this stack
rebases onto main; the extraction itself preserves the current calling shims.

Subscription application services validate the complete percent-deadband range
batch before creating any monitored item and validate every cancellation ID
before cancelling any. Offline lists and cancellation use the existing manager
without selecting a native session. Filter descriptions are native-free; SDK
enums and monitoring parameters remain in the native manager. Shared
`subscriptions-port.json` cases pin caps, options, call order, refusal frames and
buffer loss; native timeout tests retain the cause with one creation attempt.

Diagnostics use an async port even for Python’s blocking library. A running
connection round is reported without joining it. The capability snapshot is
read after the liveness operation, because recovery may replace the session.
Connection failures remain disconnected status records. Native status/namespace
and diagnostics-summary codecs retain their existing exports and service
footprints; the native adapter translates errors before retry classification
and before crossing Python’s worker-thread boundary. Shared
`diagnostics-port.json` cases pin these report and call-order rules.

The execution envelope is shared by every control tool and consumes an injected
port. The protocol adapter supplies authorization, durable audit and invocation
callbacks; the envelope imports neither MCP nor the native library. Shared
`execution-port.json` cases cover all four control declarations, refusal before
operation on policy/audit failure, normalization before failed audit, second
physical-attempt numbering and suppression of duplicate failure after a retry
denial. Connection/capability preparation and uncertain-outcome recovery now use the
shared invocation port; runtime shims provide only the underlying services. The current ordering is
preserved: bounds/schema, in-flight wait, policy, allowed audit, then connected
invocation/capability preparation; the refactor does not broaden permission.

Invocation/recovery consumes callbacks rather than a native connection or
protocol server. It reconnects once after a classified dead session, then follows
only the contract retry policy: controls report uncertainty without a second
send; reconnect-only tools propagate their failure; reads reauthorize, durably
audit the second permission, recheck the new capability generation and dispatch
once more. A second failure is never retried. Fourteen shared
`invocation-port.json` cases cover each control, recovery failures, changed
policy/capabilities and audit failure before resend. Status remains a special
bounded-warmup invocation, without connecting in the outer pipeline.

Protocol handlers and presenters now have their own modules. Python keeps the
external function signatures and reexports the legacy entry points; registration
uses an immutable catalogue of stateless functions. Node contract dispatch uses
injected handler methods. Protocol modules import no native SDK and do not
import the central server, and each is capped at 400 lines by boundary tests.
Completeness-schema and limit-ownership checks follow the extracted owners.
Native lifecycle/cache wiring lives in `adapters/opcua_lifecycle.py` and `adapters/opcua-runtime.ts`. Audit construction lives in `infrastructure/control_audit.py` and `infrastructure/control-audit.ts`; both consume plain connection facts. Python protocol lifecycle, signals, catalogue and invocation adapters have separate modules. The central entry points compose these owners and retain public compatibility exports. Tests for native history behavior target the extracted feature handler rather than manufacturing an uninitialized central server.

CI and the release conformance action enforce a per-function complexity limit of 25 for the central composition modules and extracted application, native, protocol and infrastructure modules. Python uses Ruff’s McCabe rule; Node uses Oxlint’s modified cyclomatic rule, which counts contract dispatch switches as one branch. These limits complement the 400-line import-boundary tests. Run `npm run lint:complexity --prefix packages/server-node` and `uv run ruff check --select C901 packages/server-python/src/opcua_mcp_server/{server.py,application,adapters,protocol,infrastructure}` locally.
