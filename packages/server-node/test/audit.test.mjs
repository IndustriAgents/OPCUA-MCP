// Where the control audit trail goes (issue #113).
//
// `tests/unit/test_audit.py` is the Python half and asserts the same behaviour:
// a record that carried a field on one server and not the other could not be
// read by one tool, and neither could one that was durable on one and not the
// other.
//
// What the record *contains* is asserted end to end in
// `tests/e2e/test_policy_e2e.py`, against real servers making real control
// calls. This file is about the sink.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import { AUDIT_FILE_ENV, AuditSink, describeAudit, operatorId } from "../build/audit.js";

const RECORD = { event: "opcua_mcp_policy", tool: "write_opcua_nodes", decision: "allowed" };

function tempPath(name = "audit.jsonl") {
  return join(mkdtempSync(join(tmpdir(), "opcua-audit-")), name);
}

function readLines(path) {
  return readFileSync(path, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

/** Everything written to stderr while `run` was running. */
function capturingStderr(run) {
  const lines = [];
  const original = console.error;
  console.error = (...parts) => lines.push(parts.join(" "));
  try {
    run();
  } finally {
    console.error = original;
  }
  return lines;
}

describe("the audit sink", () => {
  it("still writes stderr alone by default", () => {
    // Nothing that reads the stream today stops working.
    const lines = capturingStderr(() => new AuditSink().write(RECORD));
    assert.deepEqual(JSON.parse(lines[0]), RECORD);
  });

  it("writes a file beside stderr, not instead of it", () => {
    const path = tempPath();
    const sink = new AuditSink(path);
    const lines = capturingStderr(() => sink.write(RECORD));
    sink.close();

    assert.deepEqual(JSON.parse(lines[0]), RECORD);
    assert.deepEqual(readLines(path), [RECORD]);
  });

  it("gives each record its own line", () => {
    // Line-oriented on purpose: a collector tails this, and grep has to work.
    const path = tempPath();
    const sink = new AuditSink(path);
    capturingStderr(() => {
      for (let index = 0; index < 3; index += 1) {
        sink.write({ ...RECORD, call_id: `call-${index}` });
      }
    });
    sink.close();

    assert.deepEqual(
      readLines(path).map((record) => record.call_id),
      ["call-0", "call-1", "call-2"]
    );
  });

  it("puts a record on disk before the next call is served", () => {
    // Written synchronously, per record. A process killed between performing a
    // control call and a deferred flush would have reached the plant and lost
    // the only record of it — which is exactly what happens to an MCP server
    // when its client quits.
    const path = tempPath();
    const sink = new AuditSink(path);
    capturingStderr(() => sink.write(RECORD));
    assert.deepEqual(readLines(path), [RECORD], "not on disk until close()");
    sink.close();
  });

  it("appends across a restart rather than truncating", () => {
    // The one thing an audit file must never do.
    const path = tempPath();
    for (let index = 0; index < 2; index += 1) {
      const sink = new AuditSink(path);
      capturingStderr(() => sink.write({ ...RECORD, call_id: `run-${index}` }));
      sink.close();
    }

    assert.deepEqual(
      readLines(path).map((record) => record.call_id),
      ["run-0", "run-1"]
    );
  });

  it("treats a file it cannot open as fatal", () => {
    // Not a silent fall back to stderr. An operator who set this expects a
    // durable record; falling back would leave them believing they had one,
    // which is worse than not offering the option.
    assert.throws(
      () => new AuditSink(join(tempPath("no"), "such", "directory", "audit.jsonl")),
      new RegExp(AUDIT_FILE_ENV)
    );
  });

  it("names the sink in force in the startup line", () => {
    assert.equal(describeAudit(new AuditSink()), "audit=stderr only");
    const path = tempPath();
    const sink = new AuditSink(path);
    assert.equal(describeAudit(sink), `audit=${path}`);
    sink.close();
  });
});

describe("the operator label", () => {
  for (const [value, expected] of [
    ["line-a-hmi", "line-a-hmi"],
    ["  spaced  ", "spaced"],
    ["", null],
    ["   ", null],
  ]) {
    it(`reads ${JSON.stringify(value)} as ${JSON.stringify(expected)}`, () => {
      assert.equal(operatorId({ OPCUA_OPERATOR_ID: value }), expected);
    });
  }

  it("is null rather than invented when unset", () => {
    // This server has no notion of *who* is calling: one process, one configured
    // endpoint, whoever holds the MCP client. A name nothing verified would be
    // worse than none — it would make a record look attributable when it is not.
    assert.equal(operatorId({}), null);
  });
});
