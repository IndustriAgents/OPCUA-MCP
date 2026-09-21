"""Where the control audit trail goes (issue #113).

`packages/server-node/test/audit.test.mjs` is the Node half and asserts the same
behaviour: a record that carried a field on one server and not the other could
not be read by one tool, and neither could one that was durable on one and not
the other.

What the record *contains* is asserted end to end in
`tests/e2e/test_policy_e2e.py`, against real servers making real control calls.
This file is about the sink.
"""

from __future__ import annotations

import json
import os

import pytest
from opcua_mcp_server.audit import AUDIT_FILE_ENV, AuditSink, describe_audit, operator_id

RECORD = {"event": "opcua_mcp_policy", "tool": "write_opcua_nodes", "decision": "allowed"}


def read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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
    """Flushed per record, not per buffer.

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
    """So which one is in use is never a guess."""
    assert describe_audit(AuditSink()) == "audit=stderr only"
    path = tmp_path / "audit.jsonl"
    sink = AuditSink(str(path))
    try:
        assert describe_audit(sink) == f"audit={path}"
    finally:
        sink.close()


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
