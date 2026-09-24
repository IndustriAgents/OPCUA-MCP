# ADR 0001: Two first-class runtimes

- **Status:** Accepted
- **Date:** 2026-09-24
- **Decider:** @midhunxavier
- **Issue:** [#143](https://github.com/IndustriAgents/OPCUA-MCP/issues/143), under the
  [#151](https://github.com/IndustriAgents/OPCUA-MCP/issues/151) hardening epic
- **Settles the scope of:** [#138](https://github.com/IndustriAgents/OPCUA-MCP/issues/138),
  [#141](https://github.com/IndustriAgents/OPCUA-MCP/issues/141),
  [#144](https://github.com/IndustriAgents/OPCUA-MCP/issues/144)

## Context

The repository publishes the same MCP server twice: `opcua-mcp-server` on PyPI
(Python, `mcp` + `python-opcua`) and `opcua-mcp-server` on npm (Node,
`@modelcontextprotocol/sdk` + `node-opcua-client`). Both are documented as
interchangeable, and a user choosing between them has been told that nothing
depends on the choice.

That doubles the safety-critical surface. Every one of these exists twice, in
two languages, over two OPC UA client libraries that behave differently:
connection and retry state machines, security and certificate handling, policy
authorization and audit, history, events, alarms and subscriptions, validation,
serialization and errors, and packaging. `packages/server-node/src/tools.ts` and
`packages/server-python/src/opcua_mcp_server/server.py` are each about 2,000
lines. The history carries the cost: `fix(node): mark tool failures as errors
(#61)` and `fix(python): … (#63)` are one bug filed and fixed twice, and the most
recent review (#151) found another divergence, the Node server blocking its own
startup under infinite retry (#136), that no test compared. Writing this record
found about thirty more (#157).

A good deal of parity is already mechanical rather than a matter of discipline:

- `contract/tools.json` is the tool and resource surface — names, descriptions,
  input schemas, result shapes, every failure message, limits, retry policies,
  dead-session evidence, event fields. Both runtimes build `tools/list` from it.
- `tests/e2e/test_contract_parity.py` asserts byte-identical schemas and checks
  each runtime's real output against the declared shapes;
  `tests/e2e/test_runtime_differential.py` drives the same failing calls through
  both and compares the full text.
- Eight shared tables under `tests/fixtures/` are run by *both* unit suites
  (argument validation, history limits, node-id forms, subscription filters,
  type definitions, uncertain outcome, value bounds, value encoding).
- Every end-to-end test is parametrised over `["python", "node"]`.
- One version: `tests/unit/test_version_manifests.py` fails if the manifests
  drift, `tests/e2e/test_version_parity.py` checks what each running server
  reports, and one tag publishes both.

What was never written down is what the project *promises*: which differences
are allowed, who decides, and whether a problem in one runtime stops the other
shipping. And some differences are not avoidable — the Python stack has no AES
security policies, the two MCP SDKs are a protocol generation apart, the `.mcpb`
bundle can only be Node — so "identical" was never literally true. Without a
promise, each of #138, #141 and #144 would pick its own answer.

## Options considered

### A. Two first-class runtimes

Both pass the same behavioural conformance suite, security requirements,
artifact checks and release gates. Runtime-specific differences are versioned
and declared.

- **Buys:** no user is on a second-class package. Each runtime is the
  recommended route for someone today — the `.mcpb` bundle, the flagship Claude
  Desktop path, is Node; the smaller single-file executable is Python; Python is
  where plant integrators read and extend code. A second, independently written
  OPC UA client is insurance against either library, and the differential suite
  finds bugs in *both* — it is how the Node runtime's missing invocation-time
  capability check was found. Most of the enforcement already exists, so this
  formalises practice rather than inventing it.
- **Costs:** every behavioural and safety change is written, reviewed and tested
  twice, and a feature ships at the pace of the slower runtime. Where the two
  SDKs disagree the answer can only be the common subset — change notifications
  are offered on neither runtime today for exactly that reason. The Python stack
  sits on an unmaintained library (`python-opcua`, CVE-2022-25304 patched
  locally) until #144 lands, and the maintainer carries it at the same standard
  as Node in the meantime. The gate has to be strict, or "first-class" is a
  label.

### B. Primary runtime plus a compatibility runtime

One runtime is the reference; the other is a documented subset with its own
support statement.

- **Buys:** safety work concentrates on one implementation, and a divergence
  has an obvious oracle — the reference is right by definition. The
  compatibility runtime can lag.
- **Costs:** a weaker promise on a package that writes to PLCs, which is hard
  to state honestly: "this one is less supported" is not a comfortable property
  for a control path. There is no clean choice of primary — Node has the
  maintained OPC UA library and the `.mcpb`, Python has the audience and the
  smaller executable — and either choice demotes a published package with
  users. A subset tier also has to be documented tool by tool, which is more
  surface to keep true, not less.

### C. One production runtime

Deprecate one implementation with a migration window.

- **Buys:** half the surface, and all safety work in one place.
- **Costs:** a migration for every existing `npx`/`uvx` configuration of the
  dropped package, and the loss of the independent second client. Dropping Node
  loses the `.mcpb` (Claude Desktop supplies Node, not Python); dropping Python
  loses the ecosystem the project most wants to reach. The strongest technical
  argument for dropping Python — its unmaintained OPC UA library — is answered
  more cheaply by #144 than by deleting the runtime.

## Decision

**Model A: two first-class runtimes.** Both implementations meet the same
behavioural conformance suite, the same security requirements, the same
artifact checks and the same release gate, and ship together as one version.
Any capability difference between them is declared in
[`contract/runtime-differences.json`](../../contract/runtime-differences.json),
versioned with the release, and never accidental.

A wins because each runtime is the right answer for some user today, because
the machinery it needs mostly exists and only needed to be made binding, and
because B's asymmetric promise does not fit a tool that can move equipment. The
cost is real and is paid down by shrinking what has to be written twice — the
contract generating more (#138) and the two runtimes sharing one module map
(#141) — not by keeping one runtime to a lower standard.

## Consequences

### Public parity guarantees

For the same configuration, the same MCP call against the same OPC UA server
behaves the same on either package, except where
`contract/runtime-differences.json` says otherwise.

| Area | Guarantee | Enforced by |
|---|---|---|
| **Names** | Same tools, same resource, same capability gating, same `serverInfo.name` | `test_contract_parity.py`, `test_version_parity.py` |
| **Schemas** | Byte-identical normalised tool definitions — input schemas, output shapes, annotations — both from `contract/tools.json`, online and offline alike (the catalogue does not depend on the plant, #140) | `test_contract_parity.py`, `argument-validation.json` in both unit suites |
| **Behaviour** | Same validation, limits, capability checks, retry policy, uncertain-outcome handling, value encoding, node-id canonicalisation, policy decisions and value bounds | End-to-end suite parametrised over both runtimes; the eight shared fixture tables; `test_runtime_differential.py` |
| **Errors** | The same full error text, substituted from `contract/tools.json` → `errors`. The one part that is not the project's to word is a `{reason}` passed through from the OPC UA client library — a declared difference, which covers the library's words and nothing this project adds around them | `test_runtime_differential.py` |
| **Configuration** | Same variables, defaults and startup refusals, same `--install` / `--dry-run` / `--version` behaviour | `test_security_config.py`, `test_reconnect.py`, `test_security_startup.py`, `test_install_parity.py` |
| **Security** | One baseline: observe-only default, fail-closed control, control refused on an insecure channel without the lab override, allowlists and value bounds checked before anything is sent, control audit, server-certificate pinning, X.509 user identity, bounded inbound messages | `test_policy_e2e.py`, `test_secure_connection_e2e.py`, `test_security_startup.py`, `test_transport_limits.py` and its Node twin, `value-bounds.json`, the audit tests |
| **Performance** | The same *bounds* — `limits` and `transport` in the contract, and the same reconnect waits for the same settings. **Not** the same latency, throughput or memory: the libraries and concurrency models differ, and no suite measures them | `test_limits.py`, `test_reconnect.py` (budgets pinned against each other) |
| **Release timing** | Same version number, one tag, one gate, both registries and every artifact from the same run | `test_version_manifests.py`, the `verify` job in `publish.yml` |

Not promised: the exact wording of informational log lines on stderr (the audit
record *is* promised, and so are the startup refusals and warnings), the
individual OPC UA requests a client library chooses to make to get the same
answer, and on-disk side effects a client library has of its own. Anything this
project *chooses* on the wire — subscription lifetimes, the client's application
name — is behaviour, and where the two runtimes choose differently today that is
a divergence in [#157](https://github.com/IndustriAgents/OPCUA-MCP/issues/157),
not an exemption.

These are the guarantees. Where the code does not yet meet them is listed in
[#157](https://github.com/IndustriAgents/OPCUA-MCP/issues/157) and
[#136](https://github.com/IndustriAgents/OPCUA-MCP/issues/136); see
[Divergence](#divergence-how-it-is-reported-and-what-it-blocks).

### How runtime-specific features are represented

[`contract/runtime-differences.json`](../../contract/runtime-differences.json) is
the list, and [docs/compatibility.md](../compatibility.md#runtime-differences)
renders it for people. Every entry is a *deliberate* difference: it has an id,
the area it touches, what each runtime does, the rationale that makes it
allowed under the rules below, and optionally the issue that could retire it.
The list has no place for a known bug — an accidental divergence is fixed, not
declared (see [Divergence](#divergence-how-it-is-reported-and-what-it-blocks)).
`tests/unit/test_runtime_differences.py`
validates the shape and checks the claims that can be checked against the
repository (the policy lists, the manifests, the bundle, the registry file), so
a difference that disappears, or one that grows, fails a test instead of
leaving the list stale.

A runtime-specific *setting* is also recorded where configuration lives:
`contract/config.json` marks it with `runtimes` (read by one runtime only) or
`runtimeChoices` (one runtime accepts fewer values — the AES policies today),
which is what the generated bundle form and registry entry are built from. The
two files answer different questions — config.json *what* each runtime
accepts, runtime-differences.json *why* — and the test requires every setting
config.json marks as runtime-specific to be claimed by exactly one declared
difference, and the reverse.

The rules:

1. **A difference may add, never subtract.** An extra capability on one runtime
   (a security policy the other library lacks), a stricter input rule, or an
   extra distribution channel can be declared. A different tool name, schema,
   result shape, error text or default cannot, and nothing that weakens the
   security baseline above can.
2. **If it would change the MCP surface, it ships on neither.** A client
   written against one runtime must not behave differently against the other.
   The precedent is `notifications/resources/updated` and
   `notifications/tools/list_changed` — see
   [architecture.md](../architecture.md#why-the-subscriptions-resource-is-polled-not-pushed).
   The catalogue does not vary with the plant either (#140): a capability in
   `contract/tools.json` gates the *call* — refused with a typed error, the same
   on both — never what `tools/list` advertises, and never through a code path
   only one runtime has.
3. **Refuse, never downgrade.** A runtime asked for something only the other
   supports refuses at startup and names the other runtime, as the Python server
   does for the AES policies.
4. **A change to the list is a release note.** Adding, removing or changing an
   entry updates `docs/compatibility.md` (a test checks every id is there) and
   `CHANGELOG.md` in the same PR. The file's `schemaVersion` versions its format;
   the release tag versions its content, and the per-release conformance report
   will carry it.

### Minimum supported runtimes

| | Floor | Declared in | Tested in CI |
|---|---|---|---|
| Python | **3.10** | `requires-python = ">=3.10"` | 3.10 and 3.13 |
| Node | **22.13** | `engines.node = ">=22.13.0"`, and the `.mcpb` manifest's `compatibility.runtimes.node` | 22 and 24 |

`tests/unit/test_runtime_differences.py` fails if the manifests and this table
disagree. "Supported" means the floor and the versions CI runs; a newer release
of either (Python 3.14, for instance) is expected to work and is unverified until
it joins the matrix. Defining and testing dependency ranges is #150.

### Dependency maintenance and end of life

- **Runtimes.** A floor rises no later than the first minor release after that
  version's upstream end of life, to the oldest release still supported
  upstream. Both manifests, the `.mcpb` manifest, the CI matrix, the badges and
  the docs move in one PR, recorded under **Changed** in the changelog. The E2E
  job names are required status checks matched by exact string, so a matrix
  change needs the `main-protection` ruleset updated with it. The first one due
  is Python 3.10, whose upstream support ends in October 2026; Node 22 follows
  in April 2027.
- **Libraries.** Dependabot proposes npm and uv updates weekly and Actions
  updates monthly. A bump on either side merges only through the same gate as
  any other change. A new major of either MCP SDK is a deliberate change: it is
  checked against the protocol-generation entry in the differences list and
  against the rule that the surface stays identical.
- **Unmaintained upstream.** A dependency with no maintained release line is a
  tracked risk, not a background condition: it gets an issue with a migration
  plan, and any local mitigation is a declared difference pointing at it. That
  is `python-opcua` today — the CVE-2022-25304 chunk-reassembly patch is
  declared, and #144 is the migration. A security fix one runtime's library
  cannot receive is patched locally or the affected feature is refused on that
  runtime; it is never left as an undeclared gap.

### Required tests before either package is released

Both packages are released from one tag through one gate, so these are required
of *both* before *either* ships:

- **Lint, typecheck and unit tests** for both runtimes, including the eight
  shared fixture tables and this repository's contract, version and
  declared-differences checks (the `Lint, typecheck & unit tests` job).
- **The end-to-end suite on every leg of the CI matrix** — Python 3.10 / Node 22,
  Python 3.13 / Node 22, Python 3.13 / Node 24 — every test run against both
  runtimes, including the differential, contract-parity, security, policy,
  resilience, history, aggregate, events and Alarms & Conditions suites.
- **Artifact smoke tests** — the npm tarball, the wheel, the `.mcpb` bundle and
  both single-file executables built, installed in isolation and driven over
  MCP (`Downloadable artifacts …`) — and, at release, both executables built
  on Linux, macOS and Windows and checked to start and report the tagged
  version (`release.yml`).
- **Required-suite mode.** A gate must not pass by skipping. #142 adds
  `OPCUA_TESTS_REQUIRED=1`, under which any skipped test is a failure and every
  required test group — including `runtime: python` and `runtime: node`, each
  of the mock-backed subsystems, and both executables — must have run and
  passed, and has `publish.yml`, `release.yml` and CI run the suite through one
  shared conformance step in that mode. Until it lands, `publish.yml` never
  installs the Alarms & Conditions mock and those tests skip at release time —
  the hole #142 exists to close.

On `main`, the lint job, the three E2E legs and the artifact job are required
status checks today; the Windows and macOS unit-and-artifact job is not yet.

### Divergence: how it is reported, and what it blocks

A **divergence** is any difference a user or MCP client can observe between the
two runtimes that `contract/runtime-differences.json` does not list.

- **Report** it with the bug template, ticking *both runtimes, behaving
  differently*, with the call, what each runtime returned, and both versions. A
  divergence where one runtime is *less safe* — a refusal one runtime makes and
  the other does not, an unaudited control call — is a vulnerability: report it
  privately per [SECURITY.md](../../SECURITY.md).
- **Triage** reproduces it on both runtimes and ends in one of two places:
  - **fixed** — parity restored in both runtimes in one PR, with a regression
    test that runs on both (a row in a shared fixture table or in the
    differential suite, not two hand-written assertions); or
  - **declared** — added to the list, only if it is genuinely deliberate and
    the rules above allow it. Convenience is not a reason: a divergence is
    never declared to avoid fixing it.
- **It blocks both releases.** A version is one gate for both packages, so a
  confirmed, undeclared divergence stops the next release of *both* — not only
  the runtime at fault. A divergence that weakens the security baseline, or
  changes what reaches the plant, cannot ship at all. For anything lesser, the
  maintainer may release with it outstanding only by naming it and its issue in
  that release's changelog — a recorded, per-release waiver, which does not
  make it a declared difference and does not carry over to the next release.

**Known when this ADR was accepted.** Comparing the two runtimes side by side
while writing it found about thirty undeclared divergences, none pinned by a
test. They are collected in
[#157](https://github.com/IndustriAgents/OPCUA-MCP/issues/157) and are bugs to
be fixed under this model, not allowed differences. The most serious group
reaches the plant: for the same call, a timezone-less date, a numeric string, a
Good-subcode status or a method without typed arguments can lead the two
runtimes to write a different value or read a different time window. Each is
fixed with one semantics for both runtimes, pinned by a shared table under
`tests/fixtures/` that both unit suites run. Two more — the Node server holding
back its MCP transport under infinite retry, and the numeric reconnect settings
accepting different spellings — are
[#136](https://github.com/IndustriAgents/OPCUA-MCP/issues/136). Until these
land, the release rule above applies to them like any other divergence.
- **After a release**, the fix goes forward as a patch release of both packages
  at the same version. There are no single-runtime versions or tags. Uploads to
  npm and PyPI are separate jobs (PyPI waits for an approval), so a partial
  publish is completed by re-running the same version, never by moving one
  registry ahead of the other.

### If the support model changes

Moving to model B or C, or any other change to this promise, takes a new ADR
that supersedes this one. The runtime being demoted or retired is then:

1. announced in the changelog, both package READMEs, the root README and
   [install.md](../install.md), and on its own stderr at startup;
2. kept at this ADR's standard — full gate, security fixes — for a window of at
   least two minor releases **and** at least 90 days, whichever is longer;
3. given a migration note covering configuration (`npx`/`uvx` entries, what
   `--install` writes, `.mcpb`) and any declared difference users relied on;
4. deprecated on its registry at the end of the window (`npm deprecate`, or a
   final PyPI release saying so), never unpublished.

Signals that would justify reopening the question: one runtime's library can no
longer meet the security baseline with no migration path; parity work
repeatedly holds releases beyond the response targets below; or one package's
real-world use becomes negligible. They are reasons to write the next ADR, not
triggers that act on their own.

### Ownership and response expectations

Both stacks have the same owner, [@midhunxavier](https://github.com/midhunxavier)
(`.github/CODEOWNERS`), and the same targets — neither runtime has a lower tier.
A change to one runtime's behaviour is reviewed for the other.

| | Target |
|---|---|
| Security report | Acknowledged within a few days, as [SECURITY.md](../../SECURITY.md) says; the fix ships in both packages in one release |
| Divergence report | Triaged — reproduced on both runtimes, and confirmed or closed — within 7 days; fixed or declared before the next release |
| Dependency security advisory | Assessed within 7 days on whichever runtime it touches, through the same gate |
| Contributor PR changing behaviour | Implements both runtimes, or explains in the PR why not and declares the difference |

These are a single maintainer's commitments for a pre-1.0 project, not a
contractual SLA.

### What this settles for related work

- **#138, contract as standard JSON Schema.** One contract for both runtimes.
  Types, tool-name unions and dispatch skeletons are generated for *both* from
  the same source, and validation errors normalise to the same contract-owned
  codes on both — one generated-code rule, chosen once, for both languages. The
  differences file is a candidate for the same schema validation.
- **#141, modularisation.** Both runtimes adopt the *same* feature-module map
  (`read`, `browse`, `write`, `methods`, `history`, `events`, `alarms`,
  `subscriptions`, `diagnostics`) and the same execution-pipeline order, so a
  fix in one runtime points at the file to fix in the other. Symmetry is the
  default at module and port boundaries; library-specific code stays in each
  runtime's adapter, where it belongs.
- **#144, Python on `asyncua`.** Python migrates behind an internal OPC UA port,
  and its gate is the *existing* behavioural suite — unit, shared fixtures,
  differential and end-to-end against both runtimes — with no Python-only suite
  and no relaxed expectation. The migration may retire declared differences
  (the transport-limit patch, the certificate-naming rule, possibly the AES
  policies) and must not add undeclared ones.
- **Per-release conformance report.** A machine-readable report of what each
  release's gate ran, per runtime, is the remaining piece of #143; it builds on
  the #142 required-suite summary and carries this differences list with it.
