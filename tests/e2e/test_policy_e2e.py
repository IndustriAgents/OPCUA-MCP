"""End-to-end checks for the deployment policy boundary."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_e2e import NODE_BUILD, ROOT, connect, text_of

POLICY_ENV = {
    "OPCUA_PROFILE",
    "OPCUA_POLICY_FILE",
    "OPCUA_ALLOWED_TOOLS",
    "OPCUA_ALLOWED_WRITE_NODES",
    "OPCUA_ALLOWED_METHODS",
    "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS",
    "OPCUA_ALLOW_INSECURE_CONTROL",
    "OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL",
}

REQUIRED_OBSERVE_TOOLS = {
    "read_opcua_nodes",
    "browse_opcua_nodes",
    # Diagnostics belong in the most restricted profile there is: an
    # observe-only deployment is exactly where "is this thing even connected?"
    # has to be answerable.
    "get_server_status",
    "subscribe_opcua_nodes",
    "list_subscriptions",
    "unsubscribe_opcua_nodes",
    "subscribe_events",
    "read_events",
    "list_active_alarms",
}

#: Present only when the connected server advertises the capability behind them,
#: which is why they are optional rather than required. Both are reads, and
#: reading what already happened is the most observe-only thing there is — an
#: observe deployment is exactly where "what fired overnight" gets asked.
OPTIONAL_OBSERVE_TOOLS = {"read_opcua_history", "read_event_history"}


def observe_params(impl: str, url: str) -> StdioServerParameters:
    env = {key: value for key, value in os.environ.items() if key not in POLICY_ENV}
    env["OPCUA_SERVER_URL"] = url
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=env,
        )
    return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=env)


#: The URI the bundled mock publishes for namespace index 2, where all its nodes
#: live. Registered by the mock rather than assumed here — see its
#: `NAMESPACE_URI`.
MOCK_NAMESPACE_URI = "http://examples.freeopcua.github.io"


def uri_pinned_params(impl: str, url: str, allowed: str) -> StdioServerParameters:
    """An operator deployment whose write allowlist is spelled however `allowed` says."""
    env = {key: value for key, value in os.environ.items() if key not in POLICY_ENV}
    env.update(
        {
            "OPCUA_SERVER_URL": url,
            "OPCUA_PROFILE": "operator",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
            "OPCUA_ALLOWED_WRITE_NODES": allowed,
        }
    )
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=env,
        )
    return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=env)


def operator_params(impl: str, url: str) -> StdioServerParameters:
    env = {key: value for key, value in os.environ.items() if key not in POLICY_ENV}
    env.update(
        {
            "OPCUA_SERVER_URL": url,
            "OPCUA_PROFILE": "operator",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
            "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=13",
        }
    )
    if impl == "python":
        return StdioServerParameters(
            command="uv",
            args=["--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server"],
            env=env,
        )
    return StdioServerParameters(command="node", args=[str(NODE_BUILD)], env=env)


@pytest.fixture(params=["python", "node"])
def observe_server(request, opcua_server):
    if request.param == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    return request.param, observe_params(request.param, opcua_server)


async def test_default_profile_advertises_only_observe_tools(observe_server):
    impl, params = observe_server
    async with connect(params) as session:
        response = await session.list_tools()

    tools = {tool.name: tool for tool in response.tools}
    assert set(tools) >= REQUIRED_OBSERVE_TOOLS, impl
    assert set(tools) <= REQUIRED_OBSERVE_TOOLS | OPTIONAL_OBSERVE_TOOLS, impl
    assert tools["read_opcua_nodes"].annotations.read_only_hint is True
    assert tools["subscribe_opcua_nodes"].annotations.read_only_hint is False
    assert all(tool.annotations.destructive_hint is False for tool in tools.values())


async def test_hidden_control_tool_is_still_rejected_when_called_directly(observe_server):
    impl, params = observe_server
    async with connect(params) as session:
        result = await session.call_tool(
            "write_opcua_nodes",
            {"nodes": [{"node_id": "ns=2;i=2", "value": 999}]},
        )

    assert result.is_error is True, impl
    assert "disabled by OPCUA_PROFILE=observe" in text_of(result)


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_operator_profile_exposes_and_enforces_only_configured_targets(impl, opcua_server):
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect(operator_params(impl, opcua_server)) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        allowed = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "27.5"}]}
        )
        denied = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=12", "value": "true"}]}
        )

    assert "write_opcua_nodes" in names, impl
    assert "call_opcua_method" not in names, impl
    assert not allowed.is_error, text_of(allowed)
    assert denied.is_error is True, impl
    assert "not writable under the operator policy" in text_of(denied)


# --- the control audit trail -----------------------------------------------------


@asynccontextmanager
async def connect_capturing_stderr(params: StdioServerParameters):
    """An MCP session whose server stderr is collected — where the audit trail goes."""
    with tempfile.TemporaryFile("w+", errors="replace") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session, errlog
        errlog.seek(0)


def audit_records(errlog) -> list[dict]:
    """Every `opcua_mcp_policy` line the server wrote, parsed."""
    errlog.seek(0)
    records = []
    for line in errlog.read().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "opcua_mcp_policy":
            records.append(record)
    return records


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_the_audit_trail_records_what_a_control_call_targeted(impl, opcua_server):
    """An audit record that says a write was permitted but not *what* was written
    is not an audit trail.

    This had no test, which is how it broke: `_audit_targets` switched on tool
    *names*, so renaming the tools in 0.4.0 left every write logging
    `decision: "allowed"` with no `node_ids` at all — silently, because nothing
    looked. The targets now come from the same `guard` declaration the policy
    authorises from, so the two cannot disagree about which arguments matter.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        allowed = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "27.5"}]}
        )
        assert not allowed.is_error, text_of(allowed)
        denied = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=12", "value": "true"}]}
        )
        assert denied.is_error, impl
        records = audit_records(errlog)

    by_decision = {}
    for record in records:
        by_decision.setdefault(record["decision"], []).append(record)

    assert "allowed" in by_decision, f"{impl}: nothing recorded as allowed: {records}"
    assert "denied" in by_decision, f"{impl}: nothing recorded as denied: {records}"
    # The outcome, not only the decision: "permitted" and "happened" are
    # different facts, and the gap between them is where a control call that
    # reached the plant and then failed lives.
    assert "completed" in by_decision, f"{impl}: no outcome recorded: {records}"

    for decision in ("allowed", "completed"):
        [record] = by_decision[decision]
        assert record["tool"] == "write_opcua_nodes", record
        assert record["profile"] == "operator", record
        # Which condition let control through: here the lab override, never
        # mistakable for a verified server (#134).
        assert record["control"] == "INSECURE-OVERRIDE", record
        assert record["node_ids"] == ["ns=2;i=13"], f"{impl}: targets missing from {record}"

    [refusal] = by_decision["denied"]
    assert refusal["node_ids"] == ["ns=2;i=12"], f"{impl}: targets missing from {refusal}"
    assert "not writable under the operator policy" in refusal["reason"], refusal


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_the_audit_trail_never_records_the_value_written(impl, opcua_server):
    """Targets, never process data.

    A setpoint is what the plant is doing, and this stream is the one an MCP
    client shows the user and a log collector ships off the machine.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "31.25"}]}
        )
        records = audit_records(errlog)

    assert records, f"{impl}: nothing was audited at all"
    for record in records:
        assert "31.25" not in json.dumps(record), f"{impl}: the written value leaked: {record}"


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_reads_are_not_audited(impl, opcua_server):
    """An audit trail that recorded every read would bury the lines anyone wants."""
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        await session.call_tool("read_opcua_nodes", {"node_ids": ["ns=2;i=3"]})
        await session.call_tool("browse_opcua_nodes", {})
        records = audit_records(errlog)

    assert records == [], f"{impl}: a read was audited: {records}"


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_a_calls_audit_lines_can_be_tied_together(impl, opcua_server):
    """`allowed` and its outcome must be joinable, and distinct calls must not be.

    Each control call writes two lines, and there was nothing linking them. Both
    runtimes serve calls concurrently, so overlapping writes produced interleaved
    lines with no way to say which pairs — and two writes to the *same* node were
    not even distinguishable by content. For a trail whose whole purpose is
    "which control call reached the plant, and did it land", that was the one
    missing field.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        # The same node twice, so nothing but the id can tell the two apart.
        for value in ("21.5", "22.5"):
            result = await session.call_tool(
                "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": value}]}
            )
            assert not result.is_error, text_of(result)
        records = audit_records(errlog)

    assert all(record.get("call_id") for record in records), (
        f"{impl}: a line with no call_id cannot be joined to anything: {records}"
    )

    by_call: dict[str, list[dict]] = {}
    for record in records:
        by_call.setdefault(record["call_id"], []).append(record)

    assert len(by_call) == 2, f"{impl}: two calls must have two ids, got {sorted(by_call)}"
    for call_id, lines in by_call.items():
        decisions = {line["decision"] for line in lines}
        assert decisions == {"allowed", "completed"}, f"{impl}/{call_id}: {decisions}"
        # And a joined pair agrees about what it was: an id that spanned two
        # different calls would be worse than no id at all.
        assert len({line["tool"] for line in lines}) == 1, lines
        assert len({tuple(line["node_ids"]) for line in lines}) == 1, lines


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_every_audit_line_says_which_attempt_it_is_about(impl, opcua_server):
    """One call can reach the plant twice, and the trail has to count that.

    A request the contract marks ``retryPolicy: resend`` is sent again on a fresh
    session after an outage, and the fresh session is authorized again before it
    goes out — so "allowed" is a fact about an *attempt*, not about a call. No
    control tool is ever re-sent (every one of them is ``uncertainOutcome``), so
    on a write the number is always 1; what is pinned here is that the field is
    present and agrees across the runtimes, because a trail that carries it on
    one server and not the other cannot be read by one tool.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        allowed = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "29.5"}]}
        )
        assert not allowed.is_error, text_of(allowed)
        denied = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=12", "value": "true"}]}
        )
        assert denied.is_error, impl
        records = audit_records(errlog)

    assert records, f"{impl}: nothing was audited at all"
    for record in records:
        assert record.get("attempt") == 1, f"{impl}: no attempt number on {record}"


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_a_refusal_and_its_outcome_share_one_id(impl, opcua_server):
    """A denial is one line, and it still carries an id.

    A denied call never runs, so it has no outcome line — but it must still be
    findable by the same key as everything else, or a log search for one call id
    would quietly return nothing for exactly the calls someone is most likely to
    be searching for.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        denied = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=12", "value": "true"}]}
        )
        assert denied.is_error, impl
        records = audit_records(errlog)

    [refusal] = [record for record in records if record["decision"] == "denied"]
    assert refusal.get("call_id"), f"{impl}: a denial with no call_id: {refusal}"
    assert not [record for record in records if record["call_id"] == refusal["call_id"]][1:], (
        f"{impl}: a denied call must write exactly one line: {records}"
    )


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_the_audit_trail_says_which_plant_and_on_whose_behalf(impl, opcua_server):
    """A record a reviewer can still interpret six months later (issue #113).

    "A write to ns=2;i=5 was allowed" is not interpretable on its own: a node id
    is not stable across a server restart — that is the whole reason the `nsu=`
    allowlist form exists — and the record said nothing about where it went, over
    which session, or on whose behalf.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    params = operator_params(impl, opcua_server)
    params.env["OPCUA_OPERATOR_ID"] = "line-a-hmi"
    async with connect_capturing_stderr(params) as (session, errlog):
        result = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "27.5"}]}
        )
        assert not result.is_error, text_of(result)
        records = audit_records(errlog)

    assert records, f"{impl}: nothing was audited at all"
    for record in records:
        assert record["endpoint"] == opcua_server, f"{impl}: {record}"
        assert record["operator"] == "line-a-hmi", f"{impl}: {record}"
        assert record["session"], f"{impl}: no session on {record}"
    # One call, one session: both lines rode the same one, which is what makes
    # "were these two writes either side of an outage?" answerable.
    assert len({record["session"] for record in records}) == 1


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_an_unset_operator_is_recorded_as_unknown_rather_than_invented(impl, opcua_server):
    """This server has no notion of who is calling, and says so.

    A name nothing verified would be worse than none: it would make a record look
    attributable when it is not.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "28.5"}]}
        )
        records = audit_records(errlog)

    assert records, f"{impl}: nothing was audited at all"
    for record in records:
        assert record["operator"] is None, f"{impl}: {record}"


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_the_audit_trail_can_be_given_somewhere_durable_to_go(impl, opcua_server):
    """stderr is the MCP client's rotating log, not a compliance artifact.

    The file is the seam a collector reads. What it must contain is *exactly*
    what stderr contained — a durable copy that differed from the live stream
    would be worse than no copy.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "audit.jsonl")
        params = operator_params(impl, opcua_server)
        params.env["OPCUA_AUDIT_FILE"] = path
        async with connect_capturing_stderr(params) as (session, errlog):
            allowed = await session.call_tool(
                "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "26.5"}]}
            )
            assert not allowed.is_error, text_of(allowed)
            denied = await session.call_tool(
                "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=12", "value": "true"}]}
            )
            assert denied.is_error, impl
            from_stderr = audit_records(errlog)

        with open(path, encoding="utf-8") as handle:
            from_file = [json.loads(line) for line in handle if line.strip()]

    assert from_file == from_stderr, f"{impl}: the durable copy is not the live stream"
    assert {record["decision"] for record in from_file} == {"allowed", "completed", "denied"}


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_an_audit_file_that_cannot_be_opened_stops_the_server(impl, opcua_server):
    """Not a silent fall back to stderr.

    An operator who set this expects a durable record; starting anyway would
    leave them believing they had one, and they would find out from the absence
    of the line they went looking for.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    params = operator_params(impl, opcua_server)
    params.env["OPCUA_AUDIT_FILE"] = os.path.join(os.sep, "no", "such", "place", "audit.jsonl")

    result = subprocess.run(
        [params.command, *params.args],
        env=params.env,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )

    assert result.returncode != 0, f"{impl}: started anyway:\n{result.stderr}"
    assert "OPCUA_AUDIT_FILE" in result.stderr, f"{impl}: {result.stderr}"


#: A control call refused before authorization, for its shape or for its size.
#: The oversized value is a string past `limits.maxStringBytes`; it is refused
#: before the schema is even consulted (#139), and never reaches the plant.
REFUSED_CONTROL_CALLS = {
    "malformed": (
        {"nodes": [{"node_id": "ns=2;i=13"}]},
        "write_opcua_nodes requires nodes[0].value",
    ),
    "oversized": (
        {"nodes": [{"node_id": "ns=2;i=13", "value": "x" * 131_073}]},
        "write_opcua_nodes argument nodes[0].value is a string of 131073 bytes, over the "
        "131072-byte limit on one string (limits.maxStringBytes). Nothing was sent to the "
        "OPC UA server.",
    ),
}


@pytest.mark.parametrize("impl", ["python", "node"])
@pytest.mark.parametrize("kind", list(REFUSED_CONTROL_CALLS))
async def test_a_control_call_the_contract_refuses_is_still_audited(impl, kind, opcua_server):
    """A control call refused for its shape or its size is still a control attempt.

    Python checked both inside its audited block and Node before it, so the same
    refused write left a `denied` line on one runtime and nothing at all on the
    other (#157). Both record it now, with the refusal as the reason.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    arguments, reason = REFUSED_CONTROL_CALLS[kind]
    async with connect_capturing_stderr(operator_params(impl, opcua_server)) as (session, errlog):
        result = await session.call_tool("write_opcua_nodes", arguments)
        assert result.is_error, impl
        records = audit_records(errlog)

    assert [record["decision"] for record in records] == ["denied"], f"{impl}: {records}"
    [refusal] = records
    assert refusal["reason"] == reason, refusal
    assert refusal["node_ids"] == ["ns=2;i=13"], refusal


#: Malformed policy files and the refusal both runtimes must give at startup.
#: The full rule table is `tests/fixtures/policy-file-validation.json`, run by
#: both unit suites; these are the shapes that used to escape as a Python
#: traceback or start silently on Node, driven through the real entry points.
MALFORMED_POLICY_FILES = {
    "a callable_methods entry missing its method_id": {
        "control": {"callable_methods": [{"object_id": "ns=2;i=1"}]}
    },
    "control given as a string": {"control": "operator"},
    "a flag given as the string false": {"allow_insecure_control": "false"},
    "a profile that is not a string": {"profile": 5},
}


@pytest.mark.parametrize("shape", list(MALFORMED_POLICY_FILES))
def test_a_malformed_policy_file_stops_both_runtimes_with_the_same_first_error(
    shape, opcua_server, tmp_path
):
    """One clean `Configuration error:` line, the same on both, and no audit file.

    The audit file is the check order made visible (#157). Node opened it before
    reading the policy, so a bad policy left a freshly created, empty audit file
    behind; both now check security, policy and reconnection before creating it.
    """
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(MALFORMED_POLICY_FILES[shape]), encoding="utf-8")

    first_errors = {}
    for impl in ("python", "node"):
        audit = tmp_path / f"{impl}-audit.jsonl"
        params = operator_params(impl, opcua_server)
        params.env["OPCUA_POLICY_FILE"] = str(policy)
        params.env["OPCUA_AUDIT_FILE"] = str(audit)
        result = subprocess.run(
            [params.command, *params.args],
            env=params.env,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode == 1, f"{impl}: exited {result.returncode}:\n{result.stderr}"
        assert "Traceback" not in result.stderr, f"{impl}:\n{result.stderr}"
        assert not audit.exists(), f"{impl}: created the audit file before refusing the policy"
        errors = [
            line for line in result.stderr.splitlines() if line.startswith("Configuration error:")
        ]
        assert errors, f"{impl}: no configuration error reported:\n{result.stderr}"
        first_errors[impl] = errors[0]

    assert first_errors["python"] == first_errors["node"], first_errors


async def test_both_runtimes_write_the_same_record_shape(opcua_server):
    """One call, both servers, the same keys in the same order.

    Every other audit test here is parametrized over the runtimes and asserts the
    same things of each, which proves both satisfy the assertions and not that
    they agree — a field one server carried and the other did not would pass every
    one of them. This is the check that a log collector can read both.

    Order as well as membership, because the record is read by eye as often as by
    a parser and two servers emitting the same fields in different orders would
    make one call's two lines look like two different kinds of event.
    """
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")

    shapes = {}
    for impl in ("python", "node"):
        params = operator_params(impl, opcua_server)
        params.env["OPCUA_OPERATOR_ID"] = "line-a-hmi"
        async with connect_capturing_stderr(params) as (session, errlog):
            result = await session.call_tool(
                "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "30.5"}]}
            )
            assert not result.is_error, f"{impl}: {text_of(result)}"
            records = audit_records(errlog)
        assert records, f"{impl}: nothing was audited at all"
        shapes[impl] = [list(record) for record in records]

    assert shapes["python"] == shapes["node"], (
        f"the two runtimes write different audit records:\n"
        f"  python: {shapes['python']}\n  node:   {shapes['node']}"
    )


# --- what the audit trail can be trusted for (#146) -----------------------------


def verify_command(impl: str, *paths: str) -> list[str]:
    """`--verify-audit` on one runtime's CLI. Needs no OPC UA server."""
    if impl == "python":
        return ["uv", "--directory", str(ROOT), "run", "--no-sync", "opcua-mcp-server",
                "--verify-audit", *paths]  # fmt: skip
    return ["node", str(NODE_BUILD), "--verify-audit", *paths]


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_the_record_separates_what_was_configured_from_what_was_verified(impl, opcua_server):
    """`OPCUA_OPERATOR_ID` is a label, and the record must not dress it up.

    The configured label, the OS account, the OPC UA identity token and a
    verified remote principal are four different claims. Only the OPC UA one was
    checked by anything but this process, and there is no remote principal on a
    stdio transport at all — so that field is null whatever the label says.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    params = operator_params(impl, opcua_server)
    params.env["OPCUA_OPERATOR_ID"] = "line-a-hmi"
    async with connect_capturing_stderr(params) as (session, errlog):
        result = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "27.5"}]}
        )
        assert not result.is_error, text_of(result)
        records = audit_records(errlog)

    assert records, f"{impl}: nothing was audited at all"
    for record in records:
        assert record["schema_version"] == 2, record
        assert record["operator_label"] == "line-a-hmi", record
        assert record["operator"] == "line-a-hmi", f"{impl}: schema-1 readers lost the label"
        assert record["mcp_principal"] is None, record
        assert record["opcua_user_identity"] == {
            "type": "anonymous",
            "username": None,
            "certificate_sha256": None,
        }, record
        assert isinstance(record["process_identity"]["pid"], int), record
        if hasattr(os, "geteuid"):
            assert record["process_identity"]["uid"] == os.geteuid(), record
        assert record["session_generation"] == 1, record


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_a_control_call_is_refused_when_its_record_cannot_be_written(impl, opcua_server):
    """Fail closed for control, carry on for reads.

    The audit file is taken away mid-session — rotated out and replaced by
    something that is not a file. The next write must not reach the plant, and
    must say why; a read must still work, because monitoring is not what an audit
    outage should take down.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "audit.jsonl")
        params = operator_params(impl, opcua_server)
        params.env["OPCUA_AUDIT_FILE"] = path
        async with connect_capturing_stderr(params) as (session, _errlog):
            first = await session.call_tool(
                "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "27.5"}]}
            )
            assert not first.is_error, text_of(first)

            os.replace(path, path + ".1")
            os.mkdir(path)

            refused = await session.call_tool(
                "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": "12.345"}]}
            )
            read = await session.call_tool("read_opcua_nodes", {"node_ids": ["ns=2;i=13"]})

        with open(path + ".1", encoding="utf-8") as handle:
            recorded = [json.loads(line) for line in handle if line.strip()]

    assert refused.is_error, f"{impl}: a control call went out with no record of it"
    assert "its audit record could not be written" in text_of(refused), text_of(refused)
    assert not read.is_error, f"{impl}: a read was refused: {text_of(read)}"
    assert "12.345" not in text_of(read), f"{impl}: the refused write reached the plant"
    assert [r["decision"] for r in recorded] == ["allowed", "completed"], recorded


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_a_rotated_chained_trail_verifies_on_either_runtime(impl, opcua_server):
    """Rotation mid-session, a hash chain across it, and a verifier on each side.

    Written by one runtime and checked by *both* CLIs: a chain only one of them
    can verify is a format, not a contract.
    """
    if not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "audit.jsonl")
        rotated = path + ".1"
        params = operator_params(impl, opcua_server)
        params.env["OPCUA_AUDIT_FILE"] = path
        params.env["OPCUA_AUDIT_CHAIN"] = "sha256"
        async with connect_capturing_stderr(params) as (session, _errlog):
            for index, value in enumerate(("26.5", "25.5")):
                if index:
                    os.replace(path, rotated)
                result = await session.call_tool(
                    "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": value}]}
                )
                assert not result.is_error, text_of(result)

        if os.name == "posix":
            assert os.stat(path).st_mode & 0o777 == 0o600, f"{impl}: the new file is not 0600"
        with open(path, encoding="utf-8") as handle:
            assert [json.loads(line)["seq"] for line in handle] == [3, 4]

        for verifier in ("python", "node"):
            checked = subprocess.run(
                verify_command(verifier, rotated, path),
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
            )
            assert checked.returncode == 0, f"{impl} -> {verifier}:\n{checked.stdout}"
            assert "OK: 4 chained record(s), seq 1..4" in checked.stdout, checked.stdout

            # And the newest file alone is a chain that begins mid-way — said so,
            # not failed and not passed off as the whole story.
            alone = subprocess.run(
                verify_command(verifier, path),
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
            )
            assert alone.returncode == 0, alone.stdout
            assert "chain begins at seq 3" in alone.stdout, alone.stdout


@pytest.mark.parametrize("impl", ["python", "node"])
def test_an_unsafe_audit_target_stops_the_server(impl, opcua_server):
    """A symlink is a way to redirect the trail; it is refused, not followed."""
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    if not hasattr(os, "symlink") or os.name != "posix":
        pytest.skip("POSIX symlinks")
    with tempfile.TemporaryDirectory() as directory:
        real = os.path.join(directory, "elsewhere.jsonl")
        open(real, "w", encoding="utf-8").close()
        link = os.path.join(directory, "audit.jsonl")
        os.symlink(real, link)
        params = operator_params(impl, opcua_server)
        params.env["OPCUA_AUDIT_FILE"] = link

        result = subprocess.run(
            [params.command, *params.args],
            env=params.env,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        assert os.path.getsize(real) == 0

    assert result.returncode != 0, f"{impl}: started anyway:\n{result.stderr}"
    assert "is a symbolic link" in result.stderr, f"{impl}: {result.stderr}"


# --- the `nsu=` allowlist form, end to end --------------------------------------
#
# Untested end to end until #116, and the gap was structural rather than an
# oversight: the bundled mock created every node at a bare namespace index 2
# without publishing a URI for it, so there was no URI an `nsu=` entry could name.
# The mock registers one now.
#
# It is worth real coverage because it is the form that exists for a specific
# failure — a server that restarts and loads its namespaces in a different order,
# after which `ns=2;i=13` names a different physical node and a URI-pinned entry
# still names the right one. And it is the form that breaks if the policy object
# the connection binds a NamespaceArray into is not the object `call_tool`
# authorizes against, which is precisely what #116 moved.


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_a_uri_pinned_allowlist_entry_authorizes_the_node_it_names(impl, opcua_server):
    """`nsu=<uri>;i=13` must permit exactly what `ns=2;i=13` permits.

    The write has to succeed rather than merely be authorized: an entry that
    resolved to the wrong index would be authorized and then write somewhere else.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    allowed = f"nsu={MOCK_NAMESPACE_URI};i=13"

    async with connect(uri_pinned_params(impl, opcua_server, allowed)) as session:
        result = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": 42.5}]}
        )

    assert not result.is_error, f"{impl}: {text_of(result)}"


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_a_uri_pinned_entry_still_denies_every_other_node(impl, opcua_server):
    """The negative half, without which the test above proves nothing.

    A policy that resolved the entry to "everything", or that failed open, would
    pass the previous test and this one is what notices.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    allowed = f"nsu={MOCK_NAMESPACE_URI};i=13"

    async with connect(uri_pinned_params(impl, opcua_server, allowed)) as session:
        result = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=15", "value": 42.5}]}
        )

    assert result.is_error is True, impl
    assert "not writable" in text_of(result), f"{impl}: {text_of(result)}"


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_a_uri_this_server_does_not_publish_denies_and_says_so(impl, opcua_server):
    """Unknown denies, and loudly.

    Resolving an unknown URI to *anything* would authorize a write to whatever
    node happens to sit at that index. The refusal is the correct behaviour; the
    warning on stderr is what stops an operator believing the entry works.
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")
    allowed = "nsu=http://not.published.example/plant;i=13"

    async with connect(uri_pinned_params(impl, opcua_server, allowed)) as session:
        result = await session.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=13", "value": 42.5}]}
        )

    assert result.is_error is True, impl


@pytest.mark.parametrize("impl", ["python", "node"])
async def test_the_mock_publishes_the_namespace_its_nodes_live_in(impl, opcua_server):
    """The precondition for all of the above, asserted rather than assumed.

    If the mock ever stops publishing this URI, the three tests above would still
    pass — the first would fail, but a mock that publishes nothing would make the
    denials pass for the wrong reason. This is what distinguishes "denied because
    the policy works" from "denied because there was no namespace".
    """
    if impl == "node" and not NODE_BUILD.exists():
        pytest.skip("Node server not built")

    async with connect(observe_params(impl, opcua_server)) as session:
        result = await session.call_tool("get_server_status", {})

    namespaces = result.structured_content["result"]["namespaces"]
    at_two = next((n for n in namespaces if n["index"] == 2), None)
    assert at_two is not None, f"{impl}: no namespace at index 2: {namespaces}"
    assert at_two["uri"] == MOCK_NAMESPACE_URI, f"{impl}: {namespaces}"
