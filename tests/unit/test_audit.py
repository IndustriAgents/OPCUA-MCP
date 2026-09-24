"""Where the control audit trail goes, and what it may be trusted for (#113, #146).

`packages/server-node/test/audit.test.mjs` is the Node half and asserts the same
behaviour: a record that carried a field on one server and not the other could
not be read by one tool, and neither could one that was durable on one and not
the other. Where the two must agree to the byte — the record layout, the
configuration, the file-safety rules, the hash chain and the verifier — both are
driven from `tests/fixtures/audit.json`.

What the record *contains* for a real call is asserted end to end in
`tests/e2e/test_policy_e2e.py`, against real servers making real control calls.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from conftest import ROOT
from mcp.server.mcpserver.exceptions import ToolError
from opcua_mcp_server import audit as audit_module
from opcua_mcp_server.audit import (
    AUDIT_FILE_ENV,
    SCHEMA_VERSION,
    AuditConfig,
    AuditSink,
    AuditWriteError,
    build_record,
    chain_line,
    describe_audit,
    load_chain_key,
    opcua_user_identity,
    operator_id,
    parse_audit_config,
    process_identity,
    run_verify,
    serialize,
    unsafe_target_reason,
    verify_chain,
)
from opcua_mcp_server.errors import message
from opcua_mcp_server.policy import ToolPolicy, parse_policy_config
from opcua_mcp_server.server import create_server
from opcua_mcp_server.state import ServerState

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "audit.json").read_text(encoding="utf-8"))
KEY = FIXTURE["chain"]["key"].encode("ascii")
CHAIN_RECORDS = [build_record(**case["fields"]) for case in FIXTURE["records"]]

RECORD = {"event": "opcua_mcp_policy", "tool": "write_opcua_nodes", "decision": "allowed"}

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and owners")

#: Python opens the file without FILE_SHARE_DELETE on Windows, so a rotator cannot
#: rename or delete it while the server holds it — declared in SECURITY.md.
renamable_while_open = pytest.mark.skipif(
    os.name != "posix", reason="Windows refuses to rename a file Python holds open"
)


def read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def ids(cases):
    return [case["name"] for case in cases]


# --- the sink --------------------------------------------------------------------


def test_stderr_alone_is_still_the_default(capsys):
    """Nothing that reads the stream today stops working."""
    sink = AuditSink()
    sink.write(RECORD)

    captured = capsys.readouterr()
    assert json.loads(captured.err.strip()) == RECORD
    assert captured.out == "", "the audit trail must never touch the JSON-RPC transport"


def test_a_file_is_written_beside_stderr_not_instead_of_it(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    sink = AuditSink(str(path))
    try:
        sink.write(RECORD)
    finally:
        sink.close()

    captured = capsys.readouterr()
    assert json.loads(captured.err.strip()) == RECORD
    assert read_lines(path) == [RECORD]


def test_each_record_is_its_own_line(tmp_path):
    """Line-oriented on purpose: a collector tails this, and grep has to work."""
    path = tmp_path / "audit.jsonl"
    sink = AuditSink(str(path))
    try:
        for index in range(3):
            sink.write({**RECORD, "call_id": f"call-{index}"})
    finally:
        sink.close()

    assert [record["call_id"] for record in read_lines(path)] == ["call-0", "call-1", "call-2"]


def test_a_record_is_on_disk_before_the_next_call_is_served(tmp_path):
    """Written per record, not per buffer.

    A process killed between performing a control call and flushing would have
    reached the plant and lost the only record of it — which is the failure this
    file exists to prevent, and is exactly what happens to an MCP server when its
    client quits.
    """
    path = tmp_path / "audit.jsonl"
    sink = AuditSink(str(path))
    try:
        sink.write(RECORD)
        # Read it from a different handle, without closing the sink.
        assert read_lines(path) == [RECORD]
    finally:
        sink.close()


def test_a_restart_appends_rather_than_truncating(tmp_path):
    """The one thing an audit file must never do."""
    path = tmp_path / "audit.jsonl"
    for index in range(2):
        sink = AuditSink(str(path))
        try:
            sink.write({**RECORD, "call_id": f"run-{index}"})
        finally:
            sink.close()

    assert [record["call_id"] for record in read_lines(path)] == ["run-0", "run-1"]


def test_a_file_that_cannot_be_opened_is_fatal(tmp_path):
    """Not a silent fall back to stderr.

    An operator who set this expects a durable record. Falling back would leave
    them believing they had one, which is worse than not offering the option.
    """
    with pytest.raises(ValueError, match=AUDIT_FILE_ENV):
        AuditSink(str(tmp_path / "no" / "such" / "directory" / "audit.jsonl"))


def test_the_sink_in_force_is_named_in_the_startup_line(tmp_path):
    """So which one is in use — and how durable it is — is never a guess."""
    assert describe_audit(AuditSink()) == "audit=stderr only"
    path = tmp_path / "audit.jsonl"
    sink = AuditSink(str(path), fsync="none", chain="sha256")
    try:
        assert describe_audit(sink) == f"audit={path} fsync=none chain=sha256"
    finally:
        sink.close()


# --- the record ------------------------------------------------------------------


@pytest.mark.parametrize("case", FIXTURE["records"], ids=ids(FIXTURE["records"]))
def test_a_record_is_the_same_bytes_on_both_runtimes(case):
    record = build_record(**case["fields"])
    assert serialize(record) == case["line"]
    assert list(record)[: len(FIXTURE["field_order"])] == FIXTURE["field_order"]
    assert record["schema_version"] == FIXTURE["schema_version"]


def test_the_configured_label_is_never_presented_as_a_verified_principal():
    """`operator_label` is what was configured; `mcp_principal` is what was verified.

    Nothing is verified on a stdio transport, so the second is null whatever the
    first says — and `operator`, the schema-1 name, still carries the label so
    existing readers do not lose it.
    """
    [case] = [c for c in FIXTURE["records"] if c["fields"]["operator_label"] == "line-a-hmi"]
    record = build_record(**case["fields"])
    assert record["operator_label"] == "line-a-hmi"
    assert record["operator"] == "line-a-hmi"
    assert record["mcp_principal"] is None


@pytest.mark.parametrize("case", FIXTURE["user_identity"], ids=ids(FIXTURE["user_identity"]))
def test_the_opcua_user_identity_carries_no_secret(case, tmp_path):
    cert = None
    if case["certificate"] is not None:
        cert = tmp_path / "user.pem"
        cert.write_bytes(case["certificate"].encode("ascii"))
    assert opcua_user_identity(case["username"], str(cert) if cert else None) == case["expect"]


def test_the_process_identity_is_the_account_not_the_environment(monkeypatch):
    """`$USER` is whatever the launcher said; the uid is what the kernel says."""
    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("LOGNAME", "someone-else")
    identity = process_identity()
    assert identity["pid"] == os.getpid()
    if hasattr(os, "geteuid"):
        assert identity["uid"] == os.geteuid()
        assert identity["user"] != "someone-else" or os.geteuid() == 0


# --- the operator label ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("line-a-hmi", "line-a-hmi"), ("  spaced  ", "spaced"), ("", None), ("   ", None)],
)
def test_the_operator_label_is_taken_as_written_or_left_out(value, expected):
    assert operator_id({"OPCUA_OPERATOR_ID": value}) == expected


def test_an_unset_operator_label_is_null_rather_than_invented():
    """This server has no notion of *who* is calling.

    One process, one configured endpoint, whoever holds the MCP client. A name
    nothing verified would be worse than none — it would make a record look
    attributable when it is not.
    """
    assert operator_id({}) is None
    assert operator_id(dict(os.environ) | {"OPCUA_OPERATOR_ID": ""}) is None


# --- configuration ---------------------------------------------------------------


@pytest.mark.parametrize("case", FIXTURE["config"], ids=ids(FIXTURE["config"]))
def test_the_audit_configuration(case):
    if "error" in case:
        with pytest.raises(ValueError) as caught:
            parse_audit_config(case["env"])
        assert str(caught.value) == case["error"]
    else:
        assert parse_audit_config(case["env"]) == AuditConfig(**case["expect"])


def test_a_chain_key_must_be_long_enough(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"short\n")
    key.chmod(0o600)
    with pytest.raises(ValueError, match="holds 5 bytes; at least 32"):
        load_chain_key(str(key))


def test_a_chain_key_loses_its_trailing_newline(tmp_path):
    """`openssl rand -hex 32 > key` ends in one, and the other runtime drops it too."""
    key = tmp_path / "key"
    key.write_bytes(KEY + b"\n")
    key.chmod(0o600)
    assert load_chain_key(str(key)) == KEY


@posix_only
def test_a_chain_key_the_rest_of_the_machine_can_read_is_refused(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(KEY)
    key.chmod(0o644)
    with pytest.raises(ValueError, match="readable or writable by group or others"):
        load_chain_key(str(key))


# --- which targets are safe to write ---------------------------------------------


@pytest.mark.parametrize("case", FIXTURE["unsafe_targets"], ids=ids(FIXTURE["unsafe_targets"]))
def test_which_existing_targets_are_refused(case):
    assert (
        unsafe_target_reason(
            "/srv/audit.jsonl",
            kind=case["kind"],
            uid=case["uid"],
            mode=int(case["mode"], 8),
            euid=case["euid"],
        )
        == case["reason"]
    )


@posix_only
def test_a_new_file_is_owner_only_whatever_the_umask(tmp_path):
    """Explicit, not inherited: a permissive umask must not widen it."""
    path = tmp_path / "audit.jsonl"
    previous = os.umask(0)
    try:
        AuditSink(str(path)).close()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@posix_only
def test_an_existing_readable_file_is_used_and_left_as_it_was(tmp_path):
    """A 0640 file for a collector group is a deployment choice, not an error."""
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"")
    path.chmod(0o640)
    AuditSink(str(path)).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@posix_only
def test_a_symlink_is_refused_at_startup(tmp_path):
    real = tmp_path / "elsewhere.jsonl"
    real.write_bytes(b"")
    link = tmp_path / "audit.jsonl"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="is a symbolic link"):
        AuditSink(str(link))
    assert real.read_bytes() == b""


@posix_only
def test_a_group_writable_file_is_refused_at_startup(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"")
    path.chmod(0o660)
    with pytest.raises(ValueError, match=r"writable by group or others \(mode 0660\)"):
        AuditSink(str(path))


def test_a_directory_is_refused_at_startup(tmp_path):
    with pytest.raises(ValueError, match="is not a regular file"):
        AuditSink(str(tmp_path))


@posix_only
def test_a_fifo_is_refused_rather_than_hanging_the_server(tmp_path):
    """Opening a FIFO for writing blocks until something reads it."""
    path = tmp_path / "audit.jsonl"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="is not a regular file"):
        AuditSink(str(path))


# --- durability ------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "expected"), [("always", 3), ("none", 0)])
def test_fsync_is_per_record_unless_turned_off(tmp_path, monkeypatch, mode, expected):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"")  # existing, so the directory fsync is not counted
    sink = AuditSink(str(path), fsync=mode)
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(audit_module.os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd)))
    try:
        for _ in range(3):
            sink.write(RECORD)
    finally:
        sink.close()
    assert len(calls) == expected


# --- rotation --------------------------------------------------------------------


@renamable_while_open
def test_a_rotated_file_is_replaced_and_the_chain_carries_across(tmp_path):
    """logrotate's rename-and-create, done by anything: the next record goes to a
    new file at the path, and the chain links the two files."""
    path = tmp_path / "audit.jsonl"
    rotated = tmp_path / "audit.jsonl.1"
    sink = AuditSink(str(path), chain="sha256")
    try:
        sink.write(CHAIN_RECORDS[0])
        sink.write(CHAIN_RECORDS[1])
        os.replace(path, rotated)
        sink.write(CHAIN_RECORDS[2])
    finally:
        sink.close()

    assert [r["seq"] for r in read_lines(rotated)] == [1, 2]
    assert [r["seq"] for r in read_lines(path)] == [3]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    verdict = verify_chain([("old", rotated.read_bytes()), ("new", path.read_bytes())], key=None)
    assert verdict.ok, verdict.problems
    assert verdict.notices == []


@renamable_while_open
def test_a_deleted_file_is_recreated(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = AuditSink(str(path))
    try:
        sink.write({**RECORD, "call_id": "before"})
        path.unlink()
        sink.write({**RECORD, "call_id": "after"})
    finally:
        sink.close()
    assert [r["call_id"] for r in read_lines(path)] == ["after"]


@posix_only
def test_a_path_swapped_for_a_symlink_is_refused_and_not_written_through(tmp_path):
    """Reopen is held to the startup rules, and failing them fails the write."""
    path = tmp_path / "audit.jsonl"
    target = tmp_path / "victim"
    target.write_bytes(b"")
    sink = AuditSink(str(path))
    try:
        sink.write(RECORD)
        os.replace(path, tmp_path / "audit.jsonl.1")
        path.symlink_to(target)
        with pytest.raises(AuditWriteError, match="is a symbolic link"):
            sink.write(RECORD)
        assert target.read_bytes() == b""
        # And it recovers by itself once the path is safe again.
        path.unlink()
        sink.write({**RECORD, "call_id": "recovered"})
    finally:
        sink.close()
    assert [r.get("call_id") for r in read_lines(path)] == ["recovered"]


@renamable_while_open
def test_a_failed_write_reaches_neither_stderr_nor_the_chain(tmp_path, capsys):
    """A line saying `allowed` for a call about to be refused would be false."""
    path = tmp_path / "audit.jsonl"
    sink = AuditSink(str(path), chain="sha256")
    try:
        sink.write(CHAIN_RECORDS[0])
        os.replace(path, tmp_path / "audit.jsonl.1")
        path.mkdir()
        capsys.readouterr()
        with pytest.raises(AuditWriteError, match="is not a regular file"):
            sink.write(CHAIN_RECORDS[1])
        assert capsys.readouterr().err == ""
        path.rmdir()
        sink.write(CHAIN_RECORDS[2])
    finally:
        sink.close()
    [record] = read_lines(path)
    assert record["seq"] == 2, "the chain must not count a record that did not land"


# --- the hash chain --------------------------------------------------------------


@pytest.mark.parametrize("mode", ["sha256", "hmac-sha256"])
def test_the_chain_is_the_same_bytes_on_both_runtimes(tmp_path, mode):
    path = tmp_path / "audit.jsonl"
    key = KEY if mode == "hmac-sha256" else None
    sink = AuditSink(str(path), chain=mode, chain_key=key)
    try:
        for record in CHAIN_RECORDS:
            sink.write(record)
    finally:
        sink.close()
    # Bytes, not text: universal-newline reading would hide a CRLF written on Windows.
    assert path.read_bytes() == ("\n".join(FIXTURE["chain"][mode]) + "\n").encode("ascii")


def test_a_restarted_server_continues_the_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    for record in CHAIN_RECORDS[:2], CHAIN_RECORDS[2:]:
        sink = AuditSink(str(path), chain="hmac-sha256", chain_key=KEY)
        try:
            for item in record:
                sink.write(item)
        finally:
            sink.close()
    assert path.read_bytes() == ("\n".join(FIXTURE["chain"]["hmac-sha256"]) + "\n").encode("ascii")


def test_a_torn_last_line_is_left_for_the_verifier_and_not_glued_to(tmp_path):
    """A crash mid-write leaves a partial line. The next record starts its own
    line, links to the last whole record, and only the torn one is reported."""
    path = tmp_path / "audit.jsonl"
    lines = FIXTURE["chain"]["sha256"]
    path.write_bytes(("\n".join(lines[:2]) + "\n" + '{"event":"opcua_mcp_po').encode("ascii"))
    if os.name == "posix":
        path.chmod(0o600)
    sink = AuditSink(str(path), chain="sha256")
    try:
        sink.write(CHAIN_RECORDS[2])
    finally:
        sink.close()
    written = path.read_bytes().decode("ascii").split("\n")
    assert written[3] == lines[2]
    verdict = verify_chain([("audit.jsonl", path.read_bytes())], key=None)
    assert [p.split(": ")[0] for p in verdict.problems] == ["audit.jsonl:3"]


def _where(message_text: str) -> str:
    head = message_text.split(": ", 1)[0]
    return head if ":" in head and head.rsplit(":", 1)[1].isdigit() else message_text


def _apply(lines: list[str], edits: list[dict]) -> list[tuple[str, list[str]]]:
    files = [("audit.jsonl", list(lines))]
    for edit in edits:
        op = edit["op"]
        body = files[0][1]
        if op == "replace":
            body[edit["line"]] = body[edit["line"]].replace(edit["from"], edit["to"])
        elif op == "replace_line":
            body[edit["line"]] = edit["text"]
        elif op == "delete":
            del body[edit["line"]]
        elif op == "swap":
            i, j = edit["lines"]
            body[i], body[j] = body[j], body[i]
        elif op == "insert":
            body.insert(edit["line"], edit["text"])
        elif op == "truncate":
            del body[edit["keep"] :]
        elif op == "drop_head":
            del body[: edit["count"]]
        elif op == "split":
            files = [("a", body[: edit["at"]]), ("b", body[edit["at"] :])]
        elif op == "reverse_files":
            files.reverse()
        elif op == "crlf":
            pass  # applied when the files are joined
        else:
            raise AssertionError(f"unknown edit {op}")
    return files


@pytest.mark.parametrize("case", FIXTURE["verify"], ids=ids(FIXTURE["verify"]))
def test_the_verifier(case):
    files = _apply(FIXTURE["chain"][case["mode"]], case["edits"])
    eol = "\r\n" if any(edit["op"] == "crlf" for edit in case["edits"]) else "\n"
    contents = [(name, (eol.join(body) + eol).encode("utf-8")) for name, body in files]
    verdict = verify_chain(contents, KEY if case["key"] else None)
    assert [_where(p) for p in verdict.problems] == case["problems"]
    assert [_where(n) for n in verdict.notices] == case["notices"]
    assert verdict.ok is case["ok"]


def test_chain_line_puts_the_hash_last_and_covers_everything_before_it():
    line, digest = chain_line(CHAIN_RECORDS[0], 1, None, None)
    assert line == FIXTURE["chain"]["sha256"][0]
    assert line.endswith(f',"hash":"{digest}"}}')


def test_the_verify_command(tmp_path, capsys):
    good = tmp_path / "audit.jsonl"
    good.write_bytes(("\n".join(FIXTURE["chain"]["hmac-sha256"]) + "\n").encode("ascii"))
    key = tmp_path / "key"
    key.write_bytes(KEY)
    key.chmod(0o600)

    assert run_verify([str(good), "--key-file", str(key)]) == 0
    assert capsys.readouterr().out.strip().endswith("OK: 4 chained record(s), seq 1..4")

    assert run_verify([str(good)]) == 1, "an hmac chain must not verify without its key"
    assert "FAILED" in capsys.readouterr().out

    assert run_verify([]) == 2
    assert run_verify(["--bogus"]) == 2
    assert run_verify([str(tmp_path / "missing")]) == 2


# --- failing closed --------------------------------------------------------------

OPERATOR_ENV = {
    "OPCUA_PROFILE": "operator",
    "OPCUA_ALLOW_INSECURE_CONTROL": "true",
    "OPCUA_ALLOWED_WRITE_NODES": "ns=2;i=5",
}
WRITE = {"nodes": [{"node_id": "ns=2;i=5", "value": 1.5}]}


class _BrokenSink(AuditSink):
    """A sink whose durable copy fails for the decisions named in `failing`."""

    def __init__(self, failing: set[str]):
        super().__init__()
        self.failing = failing
        self.written: list[dict] = []

    def write(self, record):
        if record.get("decision") in self.failing:
            raise AuditWriteError("Cannot write OPCUA_AUDIT_FILE /srv/audit.jsonl: disk full")
        self.written.append(record)


def _server(sink: AuditSink):
    state = ServerState(policy=ToolPolicy(parse_policy_config(OPERATOR_ENV)), audit=sink)
    server = create_server(state)
    dispatched: list[str] = []

    async def run_tool(call, context):
        dispatched.append(call.name)
        return "ran"

    server._run_tool = run_tool
    return server, dispatched


async def test_a_control_call_whose_record_cannot_be_written_is_refused():
    sink = _BrokenSink({"allowed"})
    server, dispatched = _server(sink)

    with pytest.raises(ToolError) as refused:
        await server.call_tool("write_opcua_nodes", WRITE)

    assert dispatched == [], "the call reached the plant with no record of it"
    assert str(refused.value) == message(
        "auditUnavailable",
        tool="write_opcua_nodes",
        reason="Cannot write OPCUA_AUDIT_FILE /srv/audit.jsonl: disk full",
    )
    assert sink.written == [], "a refused call must not also be recorded as failed"


async def test_reads_carry_on_while_the_audit_trail_is_down():
    server, dispatched = _server(_BrokenSink({"allowed", "denied", "completed", "failed"}))
    assert await server.call_tool("read_opcua_nodes", {"node_ids": ["ns=2;i=5"]}) == "ran"
    assert dispatched == ["read_opcua_nodes"]


async def test_an_outcome_that_cannot_be_recorded_does_not_unmake_the_call(capsys):
    """The write happened. Reporting it as failed would invite a second one."""
    sink = _BrokenSink({"completed"})
    server, dispatched = _server(sink)

    assert await server.call_tool("write_opcua_nodes", WRITE) == "ran"
    assert dispatched == ["write_opcua_nodes"]
    assert [r["decision"] for r in sink.written] == ["allowed"]
    assert "AUDIT FAILURE" in capsys.readouterr().err


async def test_a_denial_is_still_a_denial_when_it_cannot_be_recorded():
    server, dispatched = _server(_BrokenSink({"denied"}))
    with pytest.raises(ToolError, match="not writable under the operator policy"):
        await server.call_tool(
            "write_opcua_nodes", {"nodes": [{"node_id": "ns=2;i=6", "value": 1}]}
        )
    assert dispatched == []


async def test_a_real_record_names_the_process_and_leaves_the_principal_empty():
    sink = _BrokenSink(set())
    server, _ = _server(sink)
    await server.call_tool("write_opcua_nodes", WRITE)
    [allowed, completed] = sink.written
    assert list(allowed)[: len(FIXTURE["field_order"])] == FIXTURE["field_order"]
    assert allowed["process_identity"]["pid"] == os.getpid()
    assert allowed["mcp_principal"] is None
    assert allowed["session_generation"] is None, "no session was ever established"
    assert completed["decision"] == "completed"
    assert allowed["schema_version"] == SCHEMA_VERSION
