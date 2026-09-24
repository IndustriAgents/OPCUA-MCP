// Where the control audit trail goes, and what it may be trusted for (#113, #146).
//
// `tests/unit/test_audit.py` is the Python half and asserts the same behaviour:
// a record that carried a field on one server and not the other could not be
// read by one tool, and neither could one that was durable on one and not the
// other. Where the two must agree to the byte — the record layout, the
// configuration, the file-safety rules, the hash chain and the verifier — both
// are driven from `tests/fixtures/audit.json`.
//
// What the record *contains* for a real call is asserted end to end in
// `tests/e2e/test_policy_e2e.py`, against real servers making real control
// calls.
import assert from "node:assert/strict";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmdirSync,
  statSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  AUDIT_FILE_ENV,
  AuditSink,
  AuditWriteError,
  buildRecord,
  chainLine,
  describeAudit,
  loadChainKey,
  opcuaUserIdentity,
  operatorId,
  parseAuditConfig,
  processIdentity,
  runVerify,
  serialize,
  unsafeTargetReason,
  verifyChain,
} from "../build/audit.js";
import { OpcuaConnection } from "../build/connection.js";
import { message } from "../build/errors.js";
import { ToolPolicy, parsePolicyConfig } from "../build/policy.js";
import { OpcuaTools } from "../build/tools.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURE = JSON.parse(readFileSync(join(ROOT, "tests", "fixtures", "audit.json"), "utf8"));
const KEY = Buffer.from(FIXTURE.chain.key, "ascii");
const CHAIN_RECORDS = FIXTURE.records.map((c) => buildRecord(c.fields));
const POSIX = process.platform !== "win32";

const RECORD = { event: "opcua_mcp_policy", tool: "write_opcua_nodes", decision: "allowed" };

function tempDir() {
  return mkdtempSync(join(tmpdir(), "opcua-audit-"));
}

function tempPath(name = "audit.jsonl") {
  return join(tempDir(), name);
}

function readLines(path) {
  return readFileSync(path, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function rawLines(path) {
  return readFileSync(path, "ascii").split("\n").filter(Boolean);
}

function mode(path) {
  return statSync(path).mode & 0o777;
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

function quietly(run) {
  let result;
  capturingStderr(() => {
    result = run();
  });
  return result;
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

  it("names the sink in force, and how durable it is, in the startup line", () => {
    assert.equal(describeAudit(new AuditSink()), "audit=stderr only");
    const path = tempPath();
    const sink = new AuditSink(path, { fsync: "none", chain: "sha256" });
    assert.equal(describeAudit(sink), `audit=${path} fsync=none chain=sha256`);
    sink.close();
  });
});

describe("the record", () => {
  for (const testCase of FIXTURE.records) {
    it(`is the same bytes on both runtimes: ${testCase.name}`, () => {
      const record = buildRecord(testCase.fields);
      assert.equal(serialize(record), testCase.line);
      assert.deepEqual(
        Object.keys(record).slice(0, FIXTURE.field_order.length),
        FIXTURE.field_order
      );
      assert.equal(record.schema_version, FIXTURE.schema_version);
    });
  }

  it("never presents the configured label as a verified principal", () => {
    // `operator_label` is what was configured; `mcp_principal` is what was
    // verified. Nothing is verified on a stdio transport, so the second is null
    // whatever the first says — and `operator`, the schema-1 name, still carries
    // the label so existing readers do not lose it.
    const testCase = FIXTURE.records.find((c) => c.fields.operator_label === "line-a-hmi");
    const record = buildRecord(testCase.fields);
    assert.equal(record.operator_label, "line-a-hmi");
    assert.equal(record.operator, "line-a-hmi");
    assert.equal(record.mcp_principal, null);
  });

  for (const testCase of FIXTURE.user_identity) {
    it(`names the OPC UA identity without a secret: ${testCase.name}`, () => {
      let cert = null;
      if (testCase.certificate !== null) {
        cert = tempPath("user.pem");
        writeFileSync(cert, testCase.certificate, "ascii");
      }
      assert.deepEqual(opcuaUserIdentity(testCase.username, cert), testCase.expect);
    });
  }

  it("names the process by its account, not by $USER", () => {
    const identity = processIdentity();
    assert.equal(identity.pid, process.pid);
    if (POSIX) assert.equal(identity.uid, process.geteuid());
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

describe("the audit configuration", () => {
  for (const testCase of FIXTURE.config) {
    it(testCase.name, () => {
      if ("error" in testCase) {
        assert.throws(() => parseAuditConfig(testCase.env), { message: testCase.error });
      } else {
        const { key_file: keyFile, ...rest } = testCase.expect;
        assert.deepEqual(parseAuditConfig(testCase.env), { ...rest, keyFile });
      }
    });
  }

  it("refuses a chain key too short to be secret", () => {
    const key = tempPath("key");
    writeFileSync(key, "short\n");
    chmodSync(key, 0o600);
    assert.throws(() => loadChainKey(key), /holds 5 bytes; at least 32/);
  });

  it("drops a chain key's trailing newline, as the other runtime does", () => {
    const key = tempPath("key");
    writeFileSync(key, Buffer.concat([KEY, Buffer.from("\n")]));
    chmodSync(key, 0o600);
    assert.deepEqual(loadChainKey(key), KEY);
  });

  it("refuses a chain key the rest of the machine can read", { skip: !POSIX }, () => {
    const key = tempPath("key");
    writeFileSync(key, KEY);
    chmodSync(key, 0o644);
    assert.throws(() => loadChainKey(key), /readable or writable by group or others/);
  });
});

describe("which targets are safe to write", () => {
  for (const testCase of FIXTURE.unsafe_targets) {
    it(testCase.name, () => {
      assert.equal(
        unsafeTargetReason("/srv/audit.jsonl", {
          kind: testCase.kind,
          uid: testCase.uid,
          mode: parseInt(testCase.mode, 8),
          euid: testCase.euid,
        }),
        testCase.reason
      );
    });
  }

  it("creates a new file owner-only whatever the umask", { skip: !POSIX }, () => {
    const path = tempPath();
    const previous = process.umask(0);
    try {
      new AuditSink(path).close();
    } finally {
      process.umask(previous);
    }
    assert.equal(mode(path), 0o600);
  });

  it("uses an existing readable file and leaves its mode alone", { skip: !POSIX }, () => {
    const path = tempPath();
    writeFileSync(path, "");
    chmodSync(path, 0o640);
    new AuditSink(path).close();
    assert.equal(mode(path), 0o640);
  });

  it("refuses a symlink at startup", { skip: !POSIX }, () => {
    const dir = tempDir();
    const real = join(dir, "elsewhere.jsonl");
    writeFileSync(real, "");
    const link = join(dir, "audit.jsonl");
    symlinkSync(real, link);
    assert.throws(() => new AuditSink(link), /is a symbolic link/);
    assert.equal(readFileSync(real, "utf8"), "");
  });

  it("refuses a group-writable file at startup", { skip: !POSIX }, () => {
    const path = tempPath();
    writeFileSync(path, "");
    chmodSync(path, 0o660);
    assert.throws(() => new AuditSink(path), /writable by group or others \(mode 0660\)/);
  });

  it("refuses a directory at startup", () => {
    assert.throws(() => new AuditSink(tempDir()), /is not a regular file/);
  });

  it("refuses a FIFO rather than hanging the server", { skip: !POSIX }, () => {
    const path = tempPath();
    execFileSync("mkfifo", [path]);
    assert.throws(() => new AuditSink(path), /is not a regular file/);
  });
});

describe("rotation", () => {
  it("replaces a rotated file and carries the chain across", () => {
    const dir = tempDir();
    const path = join(dir, "audit.jsonl");
    const rotated = join(dir, "audit.jsonl.1");
    const sink = new AuditSink(path, { chain: "sha256" });
    quietly(() => {
      sink.write(CHAIN_RECORDS[0]);
      sink.write(CHAIN_RECORDS[1]);
      renameSync(path, rotated);
      sink.write(CHAIN_RECORDS[2]);
    });
    sink.close();

    assert.deepEqual(
      readLines(rotated).map((r) => r.seq),
      [1, 2]
    );
    assert.deepEqual(
      readLines(path).map((r) => r.seq),
      [3]
    );
    if (POSIX) assert.equal(mode(path), 0o600);
    const verdict = verifyChain(
      [
        ["old", readFileSync(rotated)],
        ["new", readFileSync(path)],
      ],
      null
    );
    assert.ok(verdict.ok, verdict.problems.join("\n"));
    assert.deepEqual(verdict.notices, []);
  });

  it("recreates a deleted file", () => {
    const path = tempPath();
    const sink = new AuditSink(path);
    quietly(() => {
      sink.write({ ...RECORD, call_id: "before" });
      unlinkSync(path);
      sink.write({ ...RECORD, call_id: "after" });
    });
    sink.close();
    assert.deepEqual(
      readLines(path).map((r) => r.call_id),
      ["after"]
    );
  });

  it(
    "refuses a path swapped for a symlink, and does not write through it",
    { skip: !POSIX },
    () => {
      const dir = tempDir();
      const path = join(dir, "audit.jsonl");
      const target = join(dir, "victim");
      writeFileSync(target, "");
      const sink = new AuditSink(path);
      quietly(() => sink.write(RECORD));
      renameSync(path, join(dir, "audit.jsonl.1"));
      symlinkSync(target, path);
      assert.throws(
        () => quietly(() => sink.write(RECORD)),
        (error) => error instanceof AuditWriteError && /is a symbolic link/.test(error.message)
      );
      assert.equal(readFileSync(target, "utf8"), "");
      // And it recovers by itself once the path is safe again.
      unlinkSync(path);
      quietly(() => sink.write({ ...RECORD, call_id: "recovered" }));
      sink.close();
      assert.deepEqual(
        readLines(path).map((r) => r.call_id),
        ["recovered"]
      );
    }
  );

  it("sends a failed record neither to stderr nor into the chain", () => {
    // A line saying `allowed` for a call about to be refused would be false.
    const dir = tempDir();
    const path = join(dir, "audit.jsonl");
    const sink = new AuditSink(path, { chain: "sha256" });
    quietly(() => sink.write(CHAIN_RECORDS[0]));
    renameSync(path, join(dir, "audit.jsonl.1"));
    mkdirSync(path);
    const stderr = [];
    assert.throws(
      () => stderr.push(...capturingStderr(() => sink.write(CHAIN_RECORDS[1]))),
      /is not a regular file/
    );
    assert.deepEqual(stderr, []);
    rmdirSync(path);
    quietly(() => sink.write(CHAIN_RECORDS[2]));
    sink.close();
    const [record] = readLines(path);
    assert.equal(record.seq, 2, "the chain must not count a record that did not land");
  });
});

describe("the hash chain", () => {
  for (const chainMode of ["sha256", "hmac-sha256"]) {
    it(`is the same bytes on both runtimes: ${chainMode}`, () => {
      const path = tempPath();
      const sink = new AuditSink(path, {
        chain: chainMode,
        chainKey: chainMode === "hmac-sha256" ? KEY : null,
      });
      quietly(() => CHAIN_RECORDS.forEach((record) => sink.write(record)));
      sink.close();
      assert.deepEqual(rawLines(path), FIXTURE.chain[chainMode]);
    });
  }

  it("is continued by a restarted server", () => {
    const path = tempPath();
    for (const batch of [CHAIN_RECORDS.slice(0, 2), CHAIN_RECORDS.slice(2)]) {
      const sink = new AuditSink(path, { chain: "hmac-sha256", chainKey: KEY });
      quietly(() => batch.forEach((record) => sink.write(record)));
      sink.close();
    }
    assert.deepEqual(rawLines(path), FIXTURE.chain["hmac-sha256"]);
  });

  it("leaves a torn last line for the verifier and does not glue onto it", () => {
    // A crash mid-write leaves a partial line. The next record starts its own
    // line, links to the last whole record, and only the torn one is reported.
    const path = tempPath();
    const lines = FIXTURE.chain.sha256;
    writeFileSync(path, lines.slice(0, 2).join("\n") + '\n{"event":"opcua_mcp_po', {
      mode: 0o600,
    });
    const sink = new AuditSink(path, { chain: "sha256" });
    quietly(() => sink.write(CHAIN_RECORDS[2]));
    sink.close();
    assert.equal(rawLines(path)[3], lines[2]);
    const verdict = verifyChain([["audit.jsonl", readFileSync(path)]], null);
    assert.deepEqual(
      verdict.problems.map((p) => p.split(": ")[0]),
      ["audit.jsonl:3"]
    );
  });

  it("puts the hash last, covering everything before it", () => {
    const { line, digest } = chainLine(CHAIN_RECORDS[0], 1, null, null);
    assert.equal(line, FIXTURE.chain.sha256[0]);
    assert.ok(line.endsWith(`,"hash":"${digest}"}`));
  });
});

function where(text) {
  const head = text.split(": ")[0];
  return /:\d+$/.test(head) ? head : text;
}

function applyEdits(lines, edits) {
  let files = [["audit.jsonl", [...lines]]];
  for (const edit of edits) {
    const body = files[0][1];
    switch (edit.op) {
      case "replace":
        body[edit.line] = body[edit.line].replace(edit.from, edit.to);
        break;
      case "replace_line":
        body[edit.line] = edit.text;
        break;
      case "delete":
        body.splice(edit.line, 1);
        break;
      case "swap": {
        const [i, j] = edit.lines;
        [body[i], body[j]] = [body[j], body[i]];
        break;
      }
      case "insert":
        body.splice(edit.line, 0, edit.text);
        break;
      case "truncate":
        body.splice(edit.keep);
        break;
      case "drop_head":
        body.splice(0, edit.count);
        break;
      case "split":
        files = [
          ["a", body.slice(0, edit.at)],
          ["b", body.slice(edit.at)],
        ];
        break;
      case "reverse_files":
        files.reverse();
        break;
      default:
        throw new Error(`unknown edit ${edit.op}`);
    }
  }
  return files;
}

describe("the verifier", () => {
  for (const testCase of FIXTURE.verify) {
    it(testCase.name, () => {
      const files = applyEdits(FIXTURE.chain[testCase.mode], testCase.edits).map(([name, body]) => [
        name,
        Buffer.from(body.join("\n") + "\n", "utf8"),
      ]);
      const verdict = verifyChain(files, testCase.key ? KEY : null);
      assert.deepEqual(verdict.problems.map(where), testCase.problems);
      assert.deepEqual(verdict.notices.map(where), testCase.notices);
      assert.equal(verdict.ok, testCase.ok);
    });
  }

  it("is a command with exit codes a script can use", () => {
    const dir = tempDir();
    const good = join(dir, "audit.jsonl");
    writeFileSync(good, FIXTURE.chain["hmac-sha256"].join("\n") + "\n");
    const key = join(dir, "key");
    writeFileSync(key, KEY, { mode: 0o600 });
    const out = [];
    const io = { log: (m) => out.push(m), err: (m) => out.push(m) };

    assert.equal(runVerify([good, "--key-file", key], io), 0);
    assert.equal(out.at(-1), "OK: 4 chained record(s), seq 1..4");
    assert.equal(runVerify([good], io), 1, "an hmac chain must not verify without its key");
    assert.equal(runVerify([], io), 2);
    assert.equal(runVerify(["--bogus"], io), 2);
    assert.equal(runVerify([join(dir, "missing")], io), 2);
  });
});

describe("failing closed", () => {
  const OPERATOR = {
    OPCUA_PROFILE: "operator",
    OPCUA_ALLOW_INSECURE_CONTROL: "true",
    OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=5",
  };
  const WRITE = { nodes: [{ node_id: "ns=2;i=5", value: 1.5 }] };

  /** A sink whose durable copy fails for the decisions named in `failing`. */
  class BrokenSink extends AuditSink {
    constructor(failing) {
      super();
      this.failing = failing;
      this.written = [];
    }
    write(record) {
      if (this.failing.has(record.decision)) {
        throw new AuditWriteError("Cannot write OPCUA_AUDIT_FILE /srv/audit.jsonl: disk full");
      }
      this.written.push(record);
    }
  }

  function harness(sink) {
    const conn = new OpcuaConnection();
    conn.ensureConnection = async () => {};
    conn.accessHistoryDataCapability = async () => false;
    conn.serverCapabilitiesAggregateFunctions = async () => [];
    const tools = new OpcuaTools(conn, new ToolPolicy(parsePolicyConfig(OPERATOR)), sink);
    const dispatched = [];
    tools.dispatch = async (name) => {
      dispatched.push(name);
      return { content: [], structuredContent: { result: [] } };
    };
    return { tools, dispatched };
  }

  const call = (tools, name, args) =>
    quietly(() => tools.callTool({ params: { name, arguments: args } }));

  it("refuses a control call whose record cannot be written", async () => {
    const sink = new BrokenSink(new Set(["allowed"]));
    const { tools, dispatched } = harness(sink);

    const result = await call(tools, "write_opcua_nodes", WRITE);

    assert.deepEqual(dispatched, [], "the call reached the plant with no record of it");
    assert.equal(result.isError, true);
    assert.equal(
      result.content[0].text,
      message("auditUnavailable", {
        tool: "write_opcua_nodes",
        reason: "Cannot write OPCUA_AUDIT_FILE /srv/audit.jsonl: disk full",
      })
    );
    assert.deepEqual(sink.written, [], "a refused call must not also be recorded as failed");
  });

  it("lets reads carry on while the audit trail is down", async () => {
    const { tools, dispatched } = harness(
      new BrokenSink(new Set(["allowed", "denied", "completed", "failed"]))
    );
    const result = await call(tools, "read_opcua_nodes", { node_ids: ["ns=2;i=5"] });
    assert.notEqual(result.isError, true);
    assert.deepEqual(dispatched, ["read_opcua_nodes"]);
  });

  it("does not unmake a call whose outcome cannot be recorded", async () => {
    // The write happened. Reporting it as failed would invite a second one.
    const sink = new BrokenSink(new Set(["completed"]));
    const { tools, dispatched } = harness(sink);
    const result = await call(tools, "write_opcua_nodes", WRITE);
    assert.notEqual(result.isError, true);
    assert.deepEqual(dispatched, ["write_opcua_nodes"]);
    assert.deepEqual(
      sink.written.map((r) => r.decision),
      ["allowed"]
    );
  });

  it("still denies when the denial cannot be recorded", async () => {
    const { tools, dispatched } = harness(new BrokenSink(new Set(["denied"])));
    const result = await call(tools, "write_opcua_nodes", {
      nodes: [{ node_id: "ns=2;i=6", value: 1 }],
    });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /not writable under the operator policy/);
    assert.deepEqual(dispatched, []);
  });

  it("writes a real record naming the process and leaving the principal empty", async () => {
    const sink = new BrokenSink(new Set());
    const { tools } = harness(sink);
    await call(tools, "write_opcua_nodes", WRITE);
    const [allowed, completed] = sink.written;
    assert.deepEqual(
      Object.keys(allowed).slice(0, FIXTURE.field_order.length),
      FIXTURE.field_order
    );
    assert.equal(allowed.process_identity.pid, process.pid);
    assert.equal(allowed.mcp_principal, null);
    assert.equal(allowed.session_generation, null, "no session was ever established");
    assert.equal(completed.decision, "completed");
  });
});
