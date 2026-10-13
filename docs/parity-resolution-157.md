# Runtime parity resolution for #157

This maps the original 34 findings in [#157](https://github.com/IndustriAgents/OPCUA-MCP/issues/157)
to their decisions and regression evidence. Both runtimes remain first-class under
[ADR 0001](adr/0001-two-first-class-runtimes.md). The native-library differences
that remain deliberate are described in [the compatibility reference](compatibility.md#runtime-differences)
and `contract/runtime-differences.json`; they are not implicit exceptions to the contract.

| Finding | Decision | Regression evidence |
|---|---|---|
| A1–2: dates | One strict grammar, UTC for zone-less inputs, reject impossible dates | `tests/fixtures/datetime-parsing.json`; `test_datetime_parsing.py`; differential error cases |
| A3–6: write coercion | One numeric grammar, safe integer/range rules, string-only text types, strict GUID/base64 conversion | `tests/fixtures/write-coercion.json`; both Variant codec suites |
| A7–8: method arguments | Resolve datatype ancestry before sending; infer only supported scalar types when metadata is absent | `tests/fixtures/method-arguments.json`; both method argument suites |
| A9: status severity | All Good subcodes are success; preserve the server's actual status name | `tests/fixtures/status-severity.json`; both status severity suites |
| A10: operator bounds | Apply the same numeric grammar used by the write codec | `tests/fixtures/value-bounds.json`; both policy/bounds suites |
| B11: event-history dates | Preserve an actionable date refusal rather than an SDK crash | `test_datetime_parsing.py`; `tests/e2e/test_runtime_differential.py` |
| B12: aggregates | Shared unsupported-function wording and session-scoped capability answers | `test_remaining_parity.py`; differential aggregate cases |
| B13: event reconnect | Reattach event subscriptions and report the gap explicitly | `test_events.py`; `tests/e2e/test_events_e2e.py`; declared `subscription-transfer` mechanism difference |
| B14: timeouts | Classify native codes, names and bounded cause chains; retry reads once and never resend control | `tests/fixtures/connection-failures.json`; `test_retry_policy.py`; uncertain-outcome E2E cases |
| B15–17: result/refusal text | Canonical JSON keys, spacing, Unicode and numbers; identical whole/millisecond timestamp spelling | `tests/fixtures/result-text.json`; both result-text suites; declared `timestamp-precision` retains Python microseconds |
| B18: prototype keys | Standard schema validation rejects unknown own properties | `tests/fixtures/argument-validation.json`; generated validator differential tests |
| B19: errors | Remove duplicate subscription framing; strictly decode EventIds; hide unexpected crashes behind the shared tool frame | `tests/fixtures/alarm-event-id.json`; `test_unexpected_tool_errors.py`; both subscription suites; anticipated library reason wording remains declared |
| B20: browse datatype | Preserve only recognized namespace-zero datatype names for unreadable values | `test_remaining_parity.py`; `tests/fixtures/type-definitions.json` |
| B21: aggregate discovery | Require the standard node identifier and name; reject duplicates/prototype names | `tests/fixtures/capability-probes.json`; both capability probe suites |
| C22: subscription requests | Request the same lifetime, keepalive, notification and priority values | `test_subscriptions.py`; `test_events.py`; native parameter tests in both runtimes |
| C23: client identity | Same application name and requested channel lifetime; refuse certificate URI conflicts before connecting | `tests/fixtures/client-identity.json`; both client-identity suites; certificate-less URI defaults remain declared |
| D24–25: policy files | Reject malformed types/fields with the same first error | `tests/fixtures/policy-file-validation.json`; `tests/e2e/test_policy_e2e.py` |
| D26: malformed control audit | Record denied malformed control calls through the same policy path | `test_audit.py`; policy E2E cases in both runtimes |
| D27: audit bytes | Same fixed-time serialization, timestamp precision, escaping and LF output; hash the exact emitted bytes | `tests/fixtures/audit.json`; `test_audit.py` raw byte/chain comparisons |
| D28: startup | Check security, policy and reconnection before opening the audit file; shared configuration error frame | `tests/e2e/test_policy_e2e.py` malformed-policy startup cases; `test_reconnect.py` |
| D29: signals | Wake Python's event loop and clean up subscriptions on supported shutdown signals | `test_signal_wakeup.py`; signal E2E cases; platform signal support remains declared |
| E30–33: installer | Same backups and output channels; refuse unreadable/invalid UTF-8 input; preserve configuration precision and LF output | `tests/fixtures/install-cases.json`; `test_install_parity.py`; both installer suites |
| E34: CLI grammar | Same exact flags, help, missing/inline operands and errors; UTF-8/LF pipes | `tests/fixtures/cli-cases.json`; `test_cli_grammar.py` raw subprocess-byte comparisons; both CLI suites |
| F: stale test references | Reference the actual unit test location | Installer source headers reference `tests/unit/test_install_parity.py` |

The final String review also reproduced an unpaired-surrogate write difference:
Node encoded replacement characters while Python could not encode the value.
Both request boundaries now refuse unpaired surrogates in values and property
names before validation, policy or a service call. Native text codecs also refuse
them before Variant construction, including inferred method arguments. Valid
supplementary Unicode is preserved. Shared request-limit, write-coercion and
method-argument fixtures pin the refusal; invocation-boundary tests prove that
neither a feature nor a connection attempt runs. JSON result/refusal text escapes
isolated surrogates safely, and nonnumeric max-change refusals use the same JSON spelling.

The alarm extraction also exposed an alias mismatch outside the original list.
`act_on_alarm(action="acknowledge")` must retain the action tool’s result shape
and error frame; the dedicated acknowledgement tool retains its existing shape.
Shared `alarm-tool-alias.json` cases drive both actual handlers through fake native
services, including Good subcodes and failures, and assert one call per request.

This evidence covers repository fixtures, mocks and the required runtime matrix.
It does not replace separately dated real-vendor conformance evidence or the
remaining security/adapter work in #144 and #167.
