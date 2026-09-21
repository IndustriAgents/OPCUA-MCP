// Where the control audit trail goes, and what it has to carry (issue #113).
//
// `audit.py` is the Python half, and the two must produce byte-identical records
// for the same call: a trail that carries a field on one server and not the other
// cannot be read by one tool.
//
// Every `control` and `alarm-action` call writes a line here. Reads never do — a
// trail that also recorded every read would bury the four lines anyone is ever
// looking for.
//
// Two things were wrong with writing it only to stderr.
//
// **It could not answer "who", or "which plant".** The record named the call, the
// profile, the tool, the decision and the targets, and nothing about whose behalf
// it was on or what it was pointed at. A reviewer six months later reads that a
// write to `ns=2;i=5` was allowed and cannot attribute it — and `ns=2;i=5` is not
// even stable across a server restart, which is the whole reason the `nsu=`
// allowlist form exists. So every record now carries `endpoint`, `session`,
// `operator` and `attempt`.
//
// **It had nowhere durable to go.** For a stdio subprocess launched by an MCP
// client, stderr is that client's rotating log: not a compliance artifact, not
// integrity-protected, and not shippable by policy. `OPCUA_AUDIT_FILE` opens an
// append-only file, one JSON object per line, flushed per record — so a reviewer
// has something to read that does not depend on what the MCP client did with the
// process's stderr. stderr is still written either way, because things already
// read it.
//
// Deliberately not here: rotation, syslog and the Windows Event Log. An MCP
// server reimplementing `logrotate` would be a worse `logrotate` and a worse MCP
// server; a file a collector tails is the seam, and the format is stable and
// line-oriented for exactly that.
//
// What a record never carries is the *value* being written. A setpoint is process
// data, this stream is shown to a user and shipped off the machine, and a test
// asserts it on both runtimes.
import { closeSync, openSync, writeSync } from "fs";

/** Where the durable copy goes, or unset for stderr only. */
export const AUDIT_FILE_ENV = "OPCUA_AUDIT_FILE";

/** Who this server is acting for, as the deployment wants it recorded. */
export const OPERATOR_ENV = "OPCUA_OPERATOR_ID";

/** The operator label to stamp on every record, or null if unset.
 *
 * A label rather than an identity: this server has no notion of *who* is calling
 * — one process, one configured endpoint, whoever holds the MCP client — and
 * pretending otherwise would put a name on a record that nothing verified. What
 * it can honestly say is which deployment the record came from, which is what a
 * reviewer correlating several servers needs.
 */
export function operatorId(env: NodeJS.ProcessEnv = process.env): string | null {
  const raw = env[OPERATOR_ENV]?.trim();
  return raw ? raw : null;
}

/** The stderr stream, and optionally a file beside it. */
export class AuditSink {
  private handle: number | null = null;

  constructor(readonly path: string | null = null) {
    if (!path) return;
    try {
      // Append, because a restart must not truncate what the run before it
      // recorded — the one thing an audit file must never do.
      this.handle = openSync(path, "a");
    } catch (error) {
      // Fatal, and deliberately so. An operator who set this expects a durable
      // record; falling back to stderr would leave them believing they had one.
      throw new Error(
        `Cannot open ${AUDIT_FILE_ENV} ${path}: ${
          error instanceof Error ? error.message : String(error)
        }`
      );
    }
  }

  /** One record, to stderr and to the file if there is one. */
  write(record: Record<string, unknown>): void {
    const line = JSON.stringify(record);
    // stdout is reserved for the MCP stdio JSON-RPC transport; `index.ts`
    // reassigns console.log to console.error for the same reason.
    console.error(line);
    if (this.handle !== null) {
      // Synchronous, and per record. A process killed between the write and a
      // deferred flush would have performed the control call and lost the only
      // record of it, which is the failure this file exists to prevent.
      writeSync(this.handle, line + "\n");
    }
  }

  close(): void {
    if (this.handle !== null) {
      closeSync(this.handle);
      this.handle = null;
    }
  }
}

/** One line for the startup log, so the sink in force is never a guess. */
export function describeAudit(sink: AuditSink): string {
  return `audit=${sink.path ?? "stderr only"}`;
}
