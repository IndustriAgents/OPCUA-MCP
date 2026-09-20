# Roadmap: v0.4.0 — pin behaviour, not just interface

A phased plan to close the remaining correctness, security and consistency gaps
found in the 0.3.0 architecture review, and to consolidate the tool surface from
17 tools to 13. Each phase is independently committable and ends with a green
test suite — the same working style as
[`ROADMAP-0.2.0.md`](ROADMAP-0.2.0.md).

Every phase here is **code and tests only**. Nothing in this plan needs physical
equipment; the hardware gate is
[#70](https://github.com/IndustriAgents/OPCUA-MCP/issues/70) and stays open until
someone runs the suite against a vendor server.

## The one finding everything descends from

`contract/tools.json` pins names, schemas, descriptions, annotations and four
result shapes. But **10 of 17 tools declare `resultShape: null`**, and for those
the output format, error wording, defaults and clamping are two hand-written
copies that no test compares.

`tests/e2e/test_contract_parity.py` is parameterised *per implementation*: it
checks Node against the contract, then Python against the contract. It never
diffs one runtime's output against the other's. So where the contract is silent,
both runtimes pass while disagreeing — the suite proves they **advertise** the
same thing, not that they **do** the same thing.

Four confirmed divergences follow directly from that silence, and every one of
them is a place where the same operation is written twice per runtime:

| # | Divergence | Evidence |
|---|---|---|
| 1 | Python batch read reports failure as success | `server.py:666` returns `f"Error reading multiple nodes: {e}"`; `tools.ts:701` throws. The last survivor of the sweep in f8f9242 (#63) |
| 2 | Node browses short on large address spaces | `server.py:519-525` drains `ContinuationPoint`; `grep -rn browseNext packages/server-node/src/` returns nothing |
| 3 | Browse output format differs | `f"…{children_info!r}"` (Python repr) vs `JSON.stringify(…, null, 2)`. `test_mcp_e2e.py` normalises with `ast.literal_eval` — the test *encodes* the drift rather than catching it |
| 4 | Read values escape the shared codec | `readOpcuaNode` / `read_opcua_node` / `get_all_variables` stringify natively. `value-encoding.json` only feeds `records.*`, so a Boolean renders `true` vs `True` and an Int64 as node-opcua's `[hi, lo]` pair vs a plain int |

A fifth, same family: `server.py:281-284` checks no StatusCode and has no `try`,
so a bad node id yields a raw python-opcua exception message where Node yields
`Failed to read node X: …`.

**Consolidation is the fix, not a feature on top of it.** Merging the
single/batch pairs leaves one read path per runtime instead of two, and one
browse traversal instead of two. The bugs above cannot recur because there is
nowhere left for them to hide.

## Decisions (locked)

1. **No deprecated aliases for the renamed tools.** Keeping the old names alive
   means keeping the second code path alive, which is precisely what the merge
   exists to eliminate. The four removed names get a migration table in
   `CHANGELOG.md` and a minor bump. Defensible pre-1.0; revisit at 1.0.
2. **`node_ids: string[]`, never `string | string[]`.** A `oneOf` union is
   shakier across client schema validators and doubles the argument shapes the
   policy guard has to walk. An array of one reads fine to a model.
3. **No `action:` enum tools.** Models choose badly among them, and an enum tool
   cannot carry a single `accessClass`, which would break the policy layer.
4. **Two merges are refused.** `read_events` + `list_active_alarms` share
   `eventRecords` but mean different things (buffer drain vs ConditionRefresh
   snapshot). 13 is the honest floor, not a target to beat.
5. **Node search and browse-path resolution add no tools.** They become
   arguments on `browse_opcua_nodes` (Phase 7), so closing
   [#11](https://github.com/IndustriAgents/OPCUA-MCP/issues/11) does not undo
   Phase 5.
6. **Phases 1–2 ship before anything else.** They are correctness bugs on the
   target hardware and must not wait behind a breaking change.

## Target surface: 17 → 13

```
read_opcua_nodes         ← read_opcua_node + read_multiple_opcua_nodes
browse_opcua_nodes       ← browse_opcua_node_children + get_all_variables
read_opcua_history       ← read_history_opcua_node + read_aggregate_opcua_node
write_opcua_nodes        ← write_opcua_node + write_multiple_opcua_nodes
subscribe_opcua_nodes    ← subscribe_opcua_node     (one or many)
unsubscribe_opcua_nodes  ← unsubscribe_opcua_node   (one or many)

unchanged: call_opcua_method · get_server_status · list_subscriptions ·
           subscribe_events · read_events · list_active_alarms ·
           acknowledge_alarm
```

- `browse_opcua_nodes(node_id?, browse_path?, depth=1, node_class?, name_filter?, include_values=false, max_nodes?)`
  — `depth: 1` is today's `browse_opcua_node_children`; `depth: n` with
  `node_class: "Variable"` and `include_values: true` is today's
  `get_all_variables`; `depth: 0` with `browse_path` is #11's resolution; a
  `name_filter` is #11's search. One traversal, one `browseNext` loop, one
  `truncated` flag.
- `read_opcua_history(node_id, start_time?, end_time?, num_values?, aggregate_function?, processing_interval?)`
  — aggregate when `aggregate_function` is present. Capability gating moves from
  the tool to the *property*: the tool is visible when `history` probes true, and
  `aggregate_function` is stripped from the advertised schema when the server
  advertises no aggregate functions. That schema already carries the live
  function list, so this is strictly more informative than hiding a whole tool.
  **The accepted trade turned out to be unnecessary**, and a test found it: the
  aggregate mock advertises aggregates *without* `AccessHistoryDataCapability`,
  so gating the merged tool on `history` alone hid aggregates entirely — the
  opposite of the intended trade. `capability` became `capabilities`, a list
  satisfied by *any* member, and `read_opcua_history` names both. A raw read
  against an aggregate-only server is refused by that server, which is honest,
  rather than hidden here, which was not.

## Phases

| # | Phase | Closes | Risk | Release |
|---|-------|--------|------|---------|
| 1 | Python batch-read error result | #76 | low | ✅ done |
| 2 | `browseNext` on Node + explicit truncation | #75 | low | ✅ done |
| 3 | Server-certificate verification + X.509 user auth | #45, #7 | medium | ✅ done |
| 4 | Contract-declared policy guards + node-ID canonicalisation | — | medium | ✅ done |
| 5 | Tool consolidation + `resultShape` on all 13 | #8, #9 | **high** | ✅ done |
| 6 | Typed method arguments from `InputArguments` | #10 | medium | ✅ done |
| 7 | Browse-path addressing + name search | #11 | low | ✅ done |
| 8 | Contract constants + audit-trail decision | — | low | ✅ done |

### Phase 1 — Python batch-read error result

`server.py:666`: `return f"Error reading multiple nodes: {e!s}"` becomes
`raise ToolError(...) from e`, worded to match Node's
`Failed to read multiple nodes: …`. One line plus the message alignment.

**Tests.** Extend `tests/e2e/test_mcp_e2e.py` to call the tool with an
unreachable node and assert `isError` on *both* runtimes. The existing sweep
(#63) had no such test for this tool, which is why it survived.

### Phase 2 — `browseNext` on Node + explicit truncation

Node's `session.browse(nodeId)` takes the first `BrowseResult` and stops. Add a
continuation-point drain mirroring `browse_children` (`server.py:497-528`):
status-check every result including continued ones, so an expired continuation
point is an error rather than a silently truncated success.

Route **both** `browseOpcuaNodeChildren` and `getAllVariables` through the one
helper — today they each call `session.browse` separately, which is how the gap
reached two tools.

Truncation needs no work: both runtimes already append
`(truncated at max_nodes=N)` to the `get_all_variables` summary. The review's
claim that the flag was computed and never reported was wrong — checked against
`tools.ts:1089` and `server.py:944` before writing any code.

**Tests.** Unit tests on both runtimes, with a stubbed session returning a
continuation point. **There is no end-to-end test and there cannot be one:**
`python-opcua`'s *server* implements browse continuation points nowhere and
ignores `RequestedMaxReferencesPerNode`, so no mock in this repo can emit one.
That resolves the open question this phase was flagged with — and it is also the
reason the bug survived review: nothing the repo could run against would ever
have caught it. Cases are mirrored line-for-line between
`test/unit.test.mjs` and `tests/unit/test_browse.py`, including the one that
matters most — a bad status on a *continued* result must raise, not silently
return page one as a complete answer.

### Phase 3 — Server-certificate verification + X.509 user auth

Two issues, one PR: they share the certificate-loading plumbing and the same
test fixture.

**[#45](https://github.com/IndustriAgents/OPCUA-MCP/issues/45) — verify the
server's certificate.** Neither runtime checks it today; it is taken from the
endpoint description and encrypted to, whoever answered. `OPCUA_SERVER_CERT`
pins an expected certificate — python-opcua accepts
`server_certificate_path=` directly (which also removes its extra endpoint
round-trip); node-opcua takes a `serverCertificate` buffer, requiring
`node-opcua-crypto` to be promoted from a transitive to a direct dependency.
Optionally `OPCUA_TRUST_UNKNOWN_CERTS=false` on Node via an
`OPCUACertificateManager`, which has no python-opcua equivalent — so that
divergence gets documented deliberately in `docs/compatibility.md` rather than
discovered.

**[#7](https://github.com/IndustriAgents/OPCUA-MCP/issues/7) — X.509 user
authentication.** Only `Anonymous` and `UserName` identity tokens exist
(`security.ts:219-225`). Add `UserTokenType.Certificate` / python-opcua's
`load_client_certificate` + `load_private_key`. **Naming matters:** the existing
`OPCUA_CLIENT_CERT` is the *channel* certificate; the identity certificate is a
different key pair and needs a different name (`OPCUA_USER_CERT` /
`OPCUA_USER_KEY`). Conflating them is the obvious way to get this wrong.

**Tests.** `tests/fixtures/pki.py` already mints a per-session key pair. The case
that matters is the negative one: point the client at the *wrong* server
certificate and assert refusal. A pinning feature that never rejects anything is
worse than none, because it reads as protection.

### Phase 4 — Contract-declared policy guards + node-ID canonicalisation

Four defects in the safety layer, all fixed by making it contract-derived like
everything else.

**Fail-open on unknown control tools.** `policy.ts:171-187` / `policy.py:176-189`
validate arguments with an if/else chain keyed on three hardcoded tool *names*,
and `classVisible` makes any other `control` tool visible whenever
`writableNodes` is non-empty. Adding a control tool to the contract therefore
makes it callable under `operator` with **zero argument validation**. Replace
with a `guard` declaration per control tool:

```json
"guard": { "writeNodePaths": ["nodes[].node_id"] }
"guard": { "methodPaths": [{ "object": "object_node_id", "method": "method_node_id" }] }
```

Policy walks the declaration. **A `control` or `alarm-action` tool with no
`guard` is denied.** `classVisible` switches exhaustively over `ACCESS_CLASSES`
(dead at `policy.py:20` today) and denies anything unrecognised, including under
`full`.

**`ns=2;i=5` is not a stable identifier.** Namespace *indexes* are assigned per
session from the NamespaceArray, so a firmware update or reordered namespace load
can silently repoint `ns=2` at a different URI and the allowlist then authorises
writes to a different physical node. Matching is also raw set membership on
untrimmed strings (`policy.ts:191`), so `i=2253` ≢ `ns=0;i=2253`. Python has a
canonicaliser at `records.py:66-80` that the policy layer does not use, and Node
has no equivalent.

Extract it into a shared `node_ids` module used by *both* `records` and `policy`
on both runtimes, and accept allowlist entries as `nsu=<uri>;i=5`, resolved to an
index per session against the NamespaceArray that `get_server_status` already
reads. This is the one change with no cheaper substitute.

**Two smaller decisions in the same PR, both now settled.** `monitor` tools stay
outside the secure-channel gate: that gate exists to stop *control* over a
channel anyone can read or forge, and a subscription costs the server resources
but changes nothing in the plant — it is read arriving by another route, and
gating it would deny the default `observe` profile its main tool on exactly the
deployments that must watch something before they may touch it. The reasoning is
now in the code rather than implied by a missing branch. And `describePolicy`,
which printed the identical `insecure-control=enabled` for a properly secured
deployment and an active lab override, now prints `control=secured`,
`control=INSECURE-OVERRIDE` or `control=blocked`.

**One bug the tests caught during implementation.** The first draft resolved an
unresolvable `nsu=` form to a placeholder string. Two *different* unresolvable
ids then shared that placeholder and compared equal — so an allowlist entry for
an unknown URI authorised a request naming a different unknown URI. Unresolvable
now means "deny", and unresolvable allowlist entries are dropped rather than
kept.

**Tests.** A shared `tests/fixtures/node-id-forms.json` on the
`value-encoding.json` model, so neither runtime can skip a case; policy e2e
asserting default-deny for a contract tool with no `guard`.

### Phase 5 — Tool consolidation + `resultShape` on all 13 *(highest risk, highest payoff)*

The breaking change, and the systemic fix.

1. **Four new result shapes** — `nodeValues`, `nodeRefs`, `writeResults`,
   `methodResult` — bringing every one of the 13 tools under a declared shape
   (8 shapes total). The existing parity machinery then starts checking all of
   them: `test_contract_parity.py` already validates `resultShape` conformance
   and has simply had nothing to check for 10 tools. **No new test framework is
   required for this half.**
2. **A cross-runtime differential test** — the half that *is* new.
   `tests/e2e/test_runtime_differential.py` runs both runtimes against the same
   mock with the same arguments and asserts `structuredContent` is equal. Shapes
   pin each runtime to the spec; this pins them to each other, catching drift the
   shape is loose enough to permit.
3. **Delete every hand-formatted string result.** `"Node X value: 3.14"` becomes
   a record through `records` / `variant_codec`, and `value-encoding.json` is
   extended to cover the read path. Divergence 4 then dies by construction rather
   than by patch. The `ast.literal_eval` normalisation in the e2e suite goes with
   it.
4. **Closes [#8](https://github.com/IndustriAgents/OPCUA-MCP/issues/8)** —
   structured output with value, dataType, statusCode and timestamps *is* the
   `nodeValues` shape.
5. **Closes [#9](https://github.com/IndustriAgents/OPCUA-MCP/issues/9)** — an
   optional `data_type` on `write_opcua_nodes` skips the read-first inference,
   which also makes write-only nodes work. Natural in the merged writer, awkward
   in two separate ones.
6. **Migration.** A table in `CHANGELOG.md` mapping each removed name to its
   replacement, plus README and `docs/examples.md`. `server.json` and the MCP
   Registry entry are re-published at 0.4.0.

Sequenced after Phase 4 deliberately: the `guard` declarations are contract
*data*, so this phase edits JSON rather than policy code — which is the proof
that Phase 4 actually decoupled them.

### Phase 6 — Typed method arguments *(#10)*

`call_opcua_method` parses each argument float → int → string and then forces
`Double` or `String`, silently dropping `Int32`, `Boolean` and arrays. Read the
method's `InputArguments` property for the declared `DataType` / `ValueRank` per
argument and convert through the existing `convert_for_variant` /
`convertForVariant`, falling back to today's heuristic only when the metadata is
absent.

**Tests.** Needs a mock method taking a boolean and an int — check whether
`packages/mock-server/opcua_local_server.py` already exposes one before assuming
new mock work.

### Phase 7 — Browse-path addressing + name search *(#11)*

Folded into `browse_opcua_nodes` rather than added as `resolve_browse_path` and
`find_nodes_by_name`, so the surface stays at 13:

- `browse_path` — resolved segment by segment against each node's children,
  **not** through `TranslateBrowsePathsToNodeIds`. A RelativePath element carries
  a *qualified* BrowseName, so translating `/Objects/Plant/Temperature` asks for
  those names in namespace 0 — and a plant's own nodes are never in namespace 0,
  so the server answers `BadNoMatch` for a path that is plainly right. Matching
  here accepts a bare `Plant` in whatever namespace it is in, and honours an
  explicit `2:Plant`. Browsing is also universal where TranslateBrowsePaths is
  optional, so both runtimes behave the same on any server. With `depth: 0` this
  is pure resolution: the addressed node's record, including its `node_id`.
- `name_filter` — substring match over browse names during the traversal that
  already exists, bounded by the same `depth` and `max_nodes` guards.

Both reuse Phase 2's single traversal and Phase 5's `nodeRefs` shape, so this is
the smallest phase in the plan despite closing the oldest usability issue.

### Phase 8 — Contract constants + audit-trail decision

Move the duplicated literals into the contract as data, as `events.defaults`
already does correctly: the traversal caps `64` / `5000` (`tools.ts:965-966`,
`server.py:861-862`), the subscription clamps, the root default `ns=0;i=85`, and
`RequestedMaxReferencesPerNode`.

Settle the audit trail: **both**, as it turned out.

Finished, because a regression this phase found made the choice for itself. The
targets in each audit record came from an if/else on tool *names* — the last such
chain in the repository, missed when Phase 4 replaced the policy's — so renaming
the tools in Phase 5 left every control call logging `decision: "allowed"` with
**no targets at all**. Silently, because the audit trail had no test. It is now
derived from the same `guard` the policy authorises from, `decision` carries the
*outcome* (`completed` / `failed`) as well as the verdict, and six end-to-end
tests pin it on both runtimes — including that reads are never audited and that a
written value never appears.

Documented, because what it is *not* also needed saying: stderr-only, with no
persistence, which `ROADMAP.md` now states plainly along with what to do instead
(collect the stream). A built-in durable sink still has no design and is still
recorded as having none.

## Outcome

All eight phases shipped in four pull requests, released as **0.4.0**:

| PR | Phases | Closed |
|---|---|---|
| [#77](https://github.com/IndustriAgents/OPCUA-MCP/pull/77) | 1–2 | #75, #76 |
| [#78](https://github.com/IndustriAgents/OPCUA-MCP/pull/78) | 3–4 | #45, #7 |
| [#79](https://github.com/IndustriAgents/OPCUA-MCP/pull/79) | 5–7 | #8, #9, #10, #11 |
| [#80](https://github.com/IndustriAgents/OPCUA-MCP/pull/80) | 8 + release | — |

Three things this plan got wrong, all found by tests rather than by review:

1. **The truncation flag was already reported.** Phase 2 claimed
   `get_all_variables` computed it and never surfaced it. It did surface it, in
   prose. Checked before writing code; the phase note was corrected rather than
   the code.
2. **The aggregate capability trade-off was backwards.** Phase 5 accepted that a
   server with aggregates but no raw history would be offered a read it could not
   serve. The bundled aggregate mock is exactly that server, and the real effect
   was the opposite — the merged tool vanished entirely, taking aggregates with
   it. `capability` became `capabilities`, satisfied by any member.
3. **`TranslateBrowsePathsToNodeIds` cannot resolve a human-written path.** A
   RelativePath element carries a *qualified* BrowseName, so `/Objects/Plant/Temp`
   asks for those names in namespace 0 — and a plant's nodes are never in
   namespace 0. Resolution matches browse names segment by segment instead.

And one bug the new tests caught in code this plan wrote: an unresolvable `nsu=`
allowlist entry first resolved to a placeholder string, so two *different* unknown
URIs compared equal and one authorised the other.

## PR sequencing

The repository's merge gate uses strict status checks, so pull requests merge
one at a time and each must be rebased on the previous. The phase order above is
therefore also the merge order. File contention drove it as much as dependency:

| Phase | Primary files | Contends with |
|---|---|---|
| 1, 2 | `tools.ts`, `server.py` | 5, 6, 7 |
| 3 | `security.ts`, `security.py` | nothing |
| 4 | `policy.*`, `contract/tools.json` | 5, 8 |
| 5 | everything | — |
| 6, 7 | `tools.ts`, `server.py` | — |
| 8 | `contract/tools.json` | — |

Phases 3 and 4 sit between the bug fixes and the consolidation precisely because
they touch files Phase 5 does not, so the two large diffs never collide.

## Out of scope

- **[#70](https://github.com/IndustriAgents/OPCUA-MCP/issues/70) — validation
  against a real vendor server.** No code; needs equipment. It stays open as the
  only remaining non-codeable gate, and Phases 1, 2 and 4 are exactly the
  findings most likely to change behaviour when it is finally run.
- **#14, #15, #16** — closed as set aside; see
  [ROADMAP.md](../ROADMAP.md#considered-and-set-aside).
- **Per-client approval semantics for control tools** — the stated prerequisite
  for a remote transport, tracked on #14, not started here.
