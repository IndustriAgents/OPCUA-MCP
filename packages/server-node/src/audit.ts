// Where the control audit trail goes, and what it has to carry (#113, #146).
//
// `audit.py` is the Python half, and the two must produce byte-identical records
// for the same call: a trail that carries a field on one server and not the other
// cannot be read by one tool. `tests/fixtures/audit.json` is the table both
// runtimes' unit tests are driven from — the record layout, the configuration,
// the file-safety rules, the hash chain and the verifier's verdicts.
//
// Every `control` and `alarm-action` call writes a line here. Reads never do — a
// trail that also recorded every read would bury the four lines anyone is ever
// looking for.
//
// **What a record says about who.** Four different things, kept apart because
// running them together is how a record comes to claim more than anything
// checked (#146): `operator_label` is what the deployment *configured*
// (`OPCUA_OPERATOR_ID`), never verified; `process_identity` is the OS account
// this process runs as; `opcua_user_identity` is the identity token this server
// presents to the OPC UA server — its type and, for a username, the name, never a
// secret; and `mcp_principal` is reserved for a verified remote caller, which a
// stdio transport does not have, so it is always null today. `operator` is the
// schema-1 name for `operator_label` and is still written, so existing readers
// keep working.
//
// **Where it goes.** stderr always, because things already read it; and with
// `OPCUA_AUDIT_FILE`, an append-only JSON-lines file beside it. The file is the
// durable copy, and the part this module is careful about:
//
// - **Who can touch it.** Created `0600`. An existing target that is a symlink,
//   not a regular file, owned by another account, or writable by group or others
//   stops the server — each is a way for someone else to edit or redirect the
//   trail. On Windows the mode bits and owner are not checked (see SECURITY.md).
// - **When it is on disk.** `OPCUA_AUDIT_FSYNC=always` (the default) fsyncs each
//   record before the call proceeds, so an `allowed` line survives a power cut,
//   not only a crash. `none` stops at the OS page cache.
// - **Rotation.** Before every record the path is checked against the open file.
//   If a rotator renamed it away, the next record goes to a freshly created and
//   freshly validated file at the path — never to whatever was put there if it
//   fails the same checks as at startup.
// - **Tamper evidence.** `OPCUA_AUDIT_CHAIN` adds `seq`, `prev_hash` and `hash` to
//   each record: a SHA-256 chain, or HMAC-SHA256 with a key from
//   `OPCUA_AUDIT_CHAIN_KEY_FILE`. `--verify-audit` checks it. What it can and
//   cannot detect is in SECURITY.md, and it is less than "tamper-proof".
//
// **When it cannot be written.** `AuditWriteError`, and the caller fails the
// control call *closed*: an `allowed` record that did not land means the call is
// refused before anything is sent. A record that fails *after* the plant was
// touched cannot un-touch it, so that one is reported on stderr and the result is
// returned as it was — reporting a write that happened as a failure invites the
// model to send it again.
//
// What a record never carries is the *value* being written. A setpoint is process
// data, this stream is shown to a user and shipped off the machine, and a test
// asserts it on both runtimes.
import { createHash, createHmac, timingSafeEqual } from "crypto";
import {
  closeSync,
  constants,
  fstatSync,
  fsyncSync,
  lstatSync,
  openSync,
  readFileSync,
  readSync,
  statSync,
  writeSync,
  type Stats,
} from "fs";
import { userInfo } from "os";
import { dirname, resolve } from "path";

import { securityConfig } from "./security.js";

/** Where the durable copy goes, or unset for stderr only. */
export const AUDIT_FILE_ENV = "OPCUA_AUDIT_FILE";

/** Who this server is acting for, as the deployment wants it recorded. */
export const OPERATOR_ENV = "OPCUA_OPERATOR_ID";

/** Whether each file record is fsync'd before the call proceeds. */
export const AUDIT_FSYNC_ENV = "OPCUA_AUDIT_FSYNC";

/** Whether, and how, records are hash-chained. */
export const AUDIT_CHAIN_ENV = "OPCUA_AUDIT_CHAIN";

/** The HMAC key for `OPCUA_AUDIT_CHAIN=hmac-sha256`. */
export const AUDIT_CHAIN_KEY_FILE_ENV = "OPCUA_AUDIT_CHAIN_KEY_FILE";

/** Bumped from the implicit 1 of #113 when the actor fields were split (#146). */
export const SCHEMA_VERSION = 2;

export const FSYNC_MODES = ["always", "none"] as const;
export const CHAIN_MODES = ["none", "sha256", "hmac-sha256"] as const;
export type FsyncMode = (typeof FSYNC_MODES)[number];
export type ChainMode = (typeof CHAIN_MODES)[number];

/** Shorter than this and an HMAC key is guessable rather than secret. */
export const MIN_KEY_BYTES = 32;

/** How much of an existing file's tail is read to continue its chain. */
const TAIL_BYTES = 64 * 1024;

/** The trailer a chained line ends with. Matched on the raw line, which is what
 * lets either runtime verify a line the other wrote without agreeing on a JSON
 * canonicalization: the hash is over exactly the bytes before it. */
const HASH_TRAILER = /,"hash":"([0-9a-f]{64})"\}$/;

/** What Python's `bytes.strip` treats as surrounding whitespace. */
const KEY_WHITESPACE = new Set([0x20, 0x09, 0x0a, 0x0d, 0x0b, 0x0c]);

/** A record could not be made durable. Control calls fail closed on this. */
export class AuditWriteError extends Error {}

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

// --- configuration -----------------------------------------------------------

/** What `OPCUA_AUDIT_*` asked for, validated but with nothing opened yet. */
export interface AuditConfig {
  file: string | null;
  fsync: FsyncMode;
  chain: ChainMode;
  keyFile: string | null;
}

/** Read and cross-check the `OPCUA_AUDIT_*` variables. Throws on a bad one. */
export function parseAuditConfig(env: NodeJS.ProcessEnv = process.env): AuditConfig {
  const read = (name: string): string | null => env[name]?.trim() || null;
  const choice = <T extends string>(name: string, allowed: readonly T[], fallback: T): T => {
    const raw = read(name);
    if (raw === null) return fallback;
    const lowered = raw.toLowerCase() as T;
    if (!allowed.includes(lowered)) {
      throw new Error(`${name} must be one of ${allowed.join(", ")}; got ${raw}`);
    }
    return lowered;
  };

  const file = read(AUDIT_FILE_ENV);
  const fsync = choice(AUDIT_FSYNC_ENV, FSYNC_MODES, "always");
  const chain = choice(AUDIT_CHAIN_ENV, CHAIN_MODES, "none");
  const keyFile = read(AUDIT_CHAIN_KEY_FILE_ENV);

  // Refused rather than ignored, each of them: a protection that is configured
  // and silently not in force reads, in a config file, exactly like one that is.
  if (chain !== "none" && file === null) {
    throw new Error(
      `${AUDIT_CHAIN_ENV}=${chain} requires ${AUDIT_FILE_ENV}: the chain is kept in ` +
        "the file it protects"
    );
  }
  if (chain === "hmac-sha256" && keyFile === null) {
    throw new Error(`${AUDIT_CHAIN_ENV}=hmac-sha256 requires ${AUDIT_CHAIN_KEY_FILE_ENV}`);
  }
  if (keyFile !== null && chain !== "hmac-sha256") {
    throw new Error(
      `${AUDIT_CHAIN_KEY_FILE_ENV} is only used with ${AUDIT_CHAIN_ENV}=hmac-sha256; ` +
        `got ${AUDIT_CHAIN_ENV}=${chain}`
    );
  }
  return { file, fsync, chain, keyFile };
}

function octal(mode: number): string {
  return (mode & 0o7777).toString(8).padStart(4, "0");
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The HMAC key in `path`, with surrounding whitespace dropped.
 *
 * Held to the rules a private key is: a key the rest of the machine can read is
 * a key anyone on it can forge a chain with. Surrounding whitespace is dropped
 * because `openssl rand -hex 32 > key` ends in a newline, and a key that
 * silently included it would verify nowhere else.
 */
export function loadChainKey(path: string): Buffer {
  let key: Buffer;
  try {
    const info = statSync(path);
    if (!info.isFile()) {
      throw new Error(`${AUDIT_CHAIN_KEY_FILE_ENV} ${path} is not a regular file`);
    }
    if (process.platform !== "win32" && info.mode & 0o077) {
      throw new Error(
        `${AUDIT_CHAIN_KEY_FILE_ENV} ${path} is readable or writable by group or others ` +
          `(mode ${octal(info.mode)}); chmod 600 it`
      );
    }
    const raw = readFileSync(path);
    let start = 0;
    let end = raw.length;
    while (start < end && KEY_WHITESPACE.has(raw[start])) start += 1;
    while (end > start && KEY_WHITESPACE.has(raw[end - 1])) end -= 1;
    key = raw.subarray(start, end);
  } catch (error) {
    const text = describeError(error);
    throw new Error(
      text.startsWith(AUDIT_CHAIN_KEY_FILE_ENV)
        ? text
        : `Cannot read ${AUDIT_CHAIN_KEY_FILE_ENV} ${path}: ${text}`
    );
  }
  if (key.length < MIN_KEY_BYTES) {
    throw new Error(
      `${AUDIT_CHAIN_KEY_FILE_ENV} ${path} holds ${key.length} bytes; at least ` +
        `${MIN_KEY_BYTES} are required (e.g. openssl rand -hex 32)`
    );
  }
  return key;
}

// --- the record --------------------------------------------------------------

export interface ProcessIdentity {
  uid: number | null;
  user: string | null;
  pid: number;
}

export interface OpcuaUserIdentity {
  type: "anonymous" | "username" | "certificate";
  username: string | null;
  certificate_sha256: string | null;
}

/** The OS account this process runs as, and which process it is.
 *
 * The effective uid and the name the account database gives it — not `$USER`,
 * which is whatever the launching environment said. Windows has no uid; there
 * the name is the best the platform offers, and is best effort.
 */
export function processIdentity(): ProcessIdentity {
  const uid = typeof process.geteuid === "function" ? process.geteuid() : null;
  let user: string | null = null;
  try {
    user = userInfo().username;
  } catch {
    user = null;
  }
  return { uid, user, pid: process.pid };
}

/** The SHA-256 fingerprint of a PEM or DER certificate, or null if unreadable.
 *
 * The DER bytes, as every certificate tool computes a fingerprint. Parsed by hand
 * rather than through a crypto library so the Python half can do the same in the
 * same few lines and get the same answer.
 */
export function certificateSha256(path: string): string | null {
  let raw: Buffer;
  try {
    raw = readFileSync(path);
  } catch {
    return null;
  }
  const pem = /-----BEGIN CERTIFICATE-----([\s\S]*?)-----END CERTIFICATE-----/.exec(
    raw.toString("latin1")
  );
  if (pem) {
    const body = pem[1].replace(/\s+/g, "");
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(body) || body.length % 4 !== 0) return null;
    raw = Buffer.from(body, "base64");
  }
  return createHash("sha256").update(raw).digest("hex");
}

/** The identity token this server presents to the OPC UA server, minus secrets.
 *
 * What the *plant* was told, which is the one identity in the record that
 * something other than this process checked. The password and the private key
 * never appear; a username is not a secret and is what the server's own audit
 * log will name.
 */
export function opcuaUserIdentity(
  username: string | undefined | null,
  userCert: string | undefined | null
): OpcuaUserIdentity {
  if (userCert !== undefined && userCert !== null) {
    return {
      type: "certificate",
      username: null,
      certificate_sha256: certificateSha256(userCert),
    };
  }
  if (username !== undefined && username !== null) {
    return { type: "username", username, certificate_sha256: null };
  }
  return { type: "anonymous", username: null, certificate_sha256: null };
}

function configuredUserIdentity(): OpcuaUserIdentity | null {
  try {
    const config = securityConfig();
    return opcuaUserIdentity(config.username, config.userCert);
  } catch {
    return null;
  }
}

export interface RecordFields {
  timestamp: string;
  call_id: string | null;
  attempt: number;
  endpoint: string | null;
  session: string | null;
  session_generation: number | null;
  operator_label: string | null;
  process_identity: ProcessIdentity | null;
  opcua_user_identity: OpcuaUserIdentity | null;
  profile: string;
  control: string | null;
  tool: string;
  decision: string;
  targets: Record<string, unknown>;
  reason?: string | null;
}

/** One audit record, in the canonical field order.
 *
 * The order is part of the record: `audit.py` builds the same keys in the same
 * order, `tests/fixtures/audit.json` pins it, and the file is read by eye as
 * often as by a parser.
 */
export function buildRecord(fields: RecordFields): Record<string, unknown> {
  return {
    event: "opcua_mcp_policy",
    schema_version: SCHEMA_VERSION,
    timestamp: fields.timestamp,
    // Second after the timestamp, so it can be grepped for to pull one call's
    // whole story out of a shipped log.
    call_id: fields.call_id,
    // Which physical attempt this line is about: one call can reach the plant
    // twice, and a trail whose purpose is "what reached the plant" counts both.
    attempt: fields.attempt,
    // Which plant, over which of this process's sessions. A node id is not
    // stable across a server restart, so a target is only interpretable later
    // alongside these.
    endpoint: fields.endpoint,
    session: fields.session,
    session_generation: fields.session_generation,
    // Configured, not verified — see the header.
    operator_label: fields.operator_label,
    // The schema-1 name, kept so existing readers keep working.
    operator: fields.operator_label,
    // Reserved for a verified remote identity. A stdio transport has none.
    mcp_principal: null,
    process_identity: fields.process_identity,
    opcua_user_identity: fields.opcua_user_identity,
    profile: fields.profile,
    // The control gate (#134): `secured`, the lab override in force, or
    // `blocked`.
    control: fields.control,
    tool: fields.tool,
    decision: fields.decision,
    ...fields.targets,
    ...(fields.reason ? { reason: fields.reason } : {}),
  };
}

/** One record as the line both runtimes write for it.
 *
 * ASCII-only, escaping exactly what Python's `json.dumps` does by default — DEL
 * and everything above it — so the two write the same bytes for the same record,
 * not merely the same JSON.
 */
export function serialize(record: Record<string, unknown>): string {
  return JSON.stringify(record).replace(
    /[\u007f-￿]/g,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`
  );
}

// --- the hash chain ----------------------------------------------------------

function digestOf(body: Buffer | string, key: Buffer | null): string {
  return key === null
    ? createHash("sha256").update(body).digest("hex")
    : createHmac("sha256", key).update(body).digest("hex");
}

/** `record` with its chain trailer, and the hash that trailer carries.
 *
 * The hash covers the serialized record *with* `seq` and `prev_hash` and closed
 * where `hash` would start — so it pins the record's content, its position, and
 * the record before it, and needs nothing but the raw line to recompute.
 */
export function chainLine(
  record: Record<string, unknown>,
  seq: number,
  prevHash: string | null,
  key: Buffer | null
): { line: string; digest: string } {
  const body = serialize({ ...record, seq, prev_hash: prevHash });
  const digest = digestOf(Buffer.from(body, "ascii"), key);
  return { line: `${body.slice(0, -1)},"hash":"${digest}"}`, digest };
}

/** The `[seq, hash]` a restarted server continues from, if the file has one.
 *
 * The last line that parses; a torn one after it (a crash mid-write) is skipped
 * rather than chained onto, and `--verify-audit` reports it. A last record with
 * no chain fields means the file was written unchained, and the chain starts
 * afresh.
 */
function lastChainLink(tail: Buffer): [number, string] | null {
  const lines = tail.toString("latin1").split("\n").reverse();
  for (const line of lines) {
    if (!line.trim()) continue;
    let record: unknown;
    try {
      record = JSON.parse(line);
    } catch {
      continue;
    }
    if (typeof record !== "object" || record === null || Array.isArray(record)) continue;
    const { seq, hash } = record as { seq?: unknown; hash?: unknown };
    if (Number.isInteger(seq) && typeof hash === "string") return [seq as number, hash];
    return null;
  }
  return null;
}

// --- the file ----------------------------------------------------------------

/** Why an existing audit target must not be written, or null if it may be.
 *
 * `kind` is `file`, `symlink`, `directory` or `other`. `euid` is null where the
 * platform has no uid, and then neither ownership nor mode is checked: on Windows
 * the bits are not what decides access (see SECURITY.md).
 *
 * Group- or world-*readable* is allowed on purpose. A collector reading the file
 * as its own group (`0640`, as logrotate's `create` commonly sets it) is a
 * legitimate deployment; a collector that can *write* it can rewrite it.
 */
export function unsafeTargetReason(
  path: string,
  target: { kind: string; uid: number | null; mode: number; euid: number | null }
): string | null {
  if (target.kind === "symlink") {
    return `${AUDIT_FILE_ENV} ${path} is a symbolic link; point it at the real file`;
  }
  if (target.kind !== "file") return `${AUDIT_FILE_ENV} ${path} is not a regular file`;
  if (target.euid === null) return null;
  if (target.uid !== target.euid) {
    return (
      `${AUDIT_FILE_ENV} ${path} is owned by uid ${target.uid}, not by this process ` +
      `(uid ${target.euid})`
    );
  }
  if (target.mode & 0o022) {
    return (
      `${AUDIT_FILE_ENV} ${path} is writable by group or others ` +
      `(mode ${octal(target.mode)}); chmod 600 it`
    );
  }
  return null;
}

function kindOf(info: Stats): string {
  if (info.isSymbolicLink()) return "symlink";
  if (info.isFile()) return "file";
  if (info.isDirectory()) return "directory";
  return "other";
}

function euid(): number | null {
  return process.platform !== "win32" && typeof process.geteuid === "function"
    ? process.geteuid()
    : null;
}

function check(path: string, info: Stats): void {
  const reason = unsafeTargetReason(path, {
    kind: kindOf(info),
    uid: info.uid,
    mode: info.mode,
    euid: euid(),
  });
  if (reason) throw new AuditWriteError(reason);
}

function isErrno(error: unknown, code: string): boolean {
  return (error as NodeJS.ErrnoException | undefined)?.code === code;
}

/** Make a newly created file's directory entry durable, where that exists.
 *
 * Best effort: some filesystems refuse to fsync a directory, and Windows cannot
 * open one. The records themselves are fsync'd regardless.
 */
function fsyncDirectory(path: string): void {
  if (process.platform === "win32") return;
  let fd: number;
  try {
    fd = openSync(dirname(resolve(path)), "r");
  } catch {
    return;
  }
  try {
    fsyncSync(fd);
  } catch {
    // See above.
  } finally {
    closeSync(fd);
  }
}

/** The durable copy: validated, append-only, reopened on rotation. */
class AuditFile {
  private fd: number | null = null;
  private identity: [number, number] | null = null;
  /** The file did not end in a newline when opened — a torn last line — so the
   *  next record starts on its own line rather than being glued to it. */
  private needsNewline = false;

  constructor(
    readonly path: string,
    private readonly fsync: boolean
  ) {}

  /** Open (creating `0600` if absent) and return the file's tail. */
  open(): Buffer {
    let existed = true;
    let fd: number;
    let info: Stats;
    let tail = Buffer.alloc(0);
    try {
      try {
        check(this.path, lstatSync(this.path));
      } catch (error) {
        if (!isErrno(error, "ENOENT")) throw error;
        existed = false;
      }
      // O_NOFOLLOW closes the gap between the lstat above and this open;
      // O_NONBLOCK stops a FIFO swapped in there from hanging the server until
      // something reads it. Neither exists on Windows.
      const flags =
        constants.O_RDWR |
        constants.O_APPEND |
        constants.O_CREAT |
        (constants.O_NOFOLLOW ?? 0) |
        (process.platform === "win32" ? 0 : (constants.O_NONBLOCK ?? 0));
      try {
        fd = openSync(this.path, flags, 0o600);
      } catch (error) {
        if (isErrno(error, "ELOOP")) {
          throw new AuditWriteError(
            `${AUDIT_FILE_ENV} ${this.path} is a symbolic link; point it at the real file`
          );
        }
        throw error;
      }
      try {
        info = fstatSync(fd);
        // Again, on what was actually opened: O_CREAT opens an existing file as
        // happily as it creates one, so whatever raced in is checked here rather
        // than trusted.
        check(this.path, info);
        if (info.size > 0) {
          const start = Math.max(0, info.size - TAIL_BYTES);
          tail = Buffer.alloc(info.size - start);
          let read = 0;
          while (read < tail.length) {
            const n = readSync(fd, tail, read, tail.length - read, start + read);
            if (n === 0) break;
            read += n;
          }
          tail = tail.subarray(0, read);
          this.needsNewline = tail.length > 0 && tail[tail.length - 1] !== 0x0a;
        }
      } catch (error) {
        closeSync(fd);
        throw error;
      }
    } catch (error) {
      if (error instanceof AuditWriteError) throw error;
      throw new AuditWriteError(
        `Cannot open ${AUDIT_FILE_ENV} ${this.path}: ${describeError(error)}`
      );
    }
    if (!existed && this.fsync) fsyncDirectory(this.path);
    this.fd = fd;
    this.identity = [info.dev, info.ino];
    return tail;
  }

  /** The fd to write to, reopening if the path no longer names it. */
  private current(): number {
    if (this.fd !== null) {
      let moved: boolean;
      try {
        const info = lstatSync(this.path);
        moved =
          this.identity === null || info.dev !== this.identity[0] || info.ino !== this.identity[1];
      } catch (error) {
        if (!isErrno(error, "ENOENT")) {
          throw new AuditWriteError(
            `Cannot stat ${AUDIT_FILE_ENV} ${this.path}: ${describeError(error)}`
          );
        }
        moved = true;
      }
      if (!moved) return this.fd;
      // Rotated away, deleted, or replaced. Whatever is at the path now is held
      // to the startup rules; the rotated file is not written again.
      this.close();
    }
    this.open();
    return this.fd as unknown as number;
  }

  write(data: Buffer): void {
    const fd = this.current();
    const bytes = this.needsNewline ? Buffer.concat([Buffer.from("\n"), data]) : data;
    try {
      let written = 0;
      while (written < bytes.length) {
        written += writeSync(fd, bytes, written, bytes.length - written);
      }
      if (this.fsync) fsyncSync(fd);
    } catch (error) {
      throw new AuditWriteError(
        `Cannot write ${AUDIT_FILE_ENV} ${this.path}: ${describeError(error)}`
      );
    }
    this.needsNewline = false;
  }

  close(): void {
    if (this.fd !== null) {
      try {
        closeSync(this.fd);
      } finally {
        this.fd = null;
        this.identity = null;
      }
    }
  }
}

/** Somewhere a finished audit line can go — the external-sink seam.
 *
 * A syslog, journald, Windows Event Log or collector sink implements this and is
 * handed to `AuditSink`. The contract, which the file sink follows:
 *
 * - `write` is synchronous and returns only once the line is as durable as that
 *   sink promises. There is no queue in front of it, so there is nothing
 *   unbounded to report on; a sink that buffers must bound the buffer itself and
 *   throw when it is full.
 * - `write` throws `AuditWriteError` when the line did not land. For a
 *   pre-dispatch record that refuses the control call.
 * - Every sink receives the same bytes — chain trailer included — so any one of
 *   them can be verified on its own.
 */
export interface LineSink {
  write(line: string): void;
  close(): void;
}

export interface AuditSinkOptions {
  fsync?: FsyncMode;
  chain?: ChainMode;
  chainKey?: Buffer | null;
  sinks?: LineSink[];
}

/** The stderr stream, optionally a durable file, and any further line sinks. */
export class AuditSink {
  readonly fsync: FsyncMode;
  readonly chain: ChainMode;
  private readonly key: Buffer | null;
  private readonly sinks: LineSink[];
  private readonly file: AuditFile | null = null;
  private seq = 0;
  private prevHash: string | null = null;
  private identity_: {
    process_identity: ProcessIdentity;
    opcua_user_identity: OpcuaUserIdentity | null;
  } | null = null;

  constructor(
    readonly path: string | null = null,
    options: AuditSinkOptions = {}
  ) {
    this.fsync = options.fsync ?? "always";
    this.chain = options.chain ?? "none";
    if (!CHAIN_MODES.includes(this.chain)) {
      throw new Error(`${AUDIT_CHAIN_ENV} must be one of ${CHAIN_MODES.join(", ")}`);
    }
    if (this.chain === "hmac-sha256" && !options.chainKey) {
      throw new Error(`${AUDIT_CHAIN_ENV}=hmac-sha256 requires a key`);
    }
    if (this.chain !== "none" && !path) {
      throw new Error(`${AUDIT_CHAIN_ENV}=${this.chain} requires ${AUDIT_FILE_ENV}`);
    }
    this.key = this.chain === "hmac-sha256" ? (options.chainKey ?? null) : null;
    this.sinks = options.sinks ?? [];
    if (!path) return;
    this.file = new AuditFile(path, this.fsync === "always");
    // Fatal, and deliberately so. An operator who set this expects a durable
    // record; falling back to stderr would leave them believing they had one.
    const tail = this.file.open();
    if (this.chain !== "none") {
      // A restart continues the file's chain rather than starting a second one
      // in the middle of it.
      const link = lastChainLink(tail);
      if (link !== null) [this.seq, this.prevHash] = link;
    }
  }

  static fromConfig(config: AuditConfig): AuditSink {
    const key = config.keyFile ? loadChainKey(config.keyFile) : null;
    return new AuditSink(config.file, { fsync: config.fsync, chain: config.chain, chainKey: key });
  }

  /** `process_identity` and `opcua_user_identity`, worked out once. */
  identity(): { process_identity: ProcessIdentity; opcua_user_identity: OpcuaUserIdentity | null } {
    if (this.identity_ === null) {
      this.identity_ = {
        process_identity: processIdentity(),
        opcua_user_identity: configuredUserIdentity(),
      };
    }
    return this.identity_;
  }

  /** One record: to the file (and any further sinks), then to stderr.
   *
   * Throws `AuditWriteError` if a durable sink did not take it. Then nothing goes
   * to stderr as a record either — a line there saying `allowed` for a call that
   * is about to be refused would be the one record in the trail that is false —
   * and the chain does not advance, so the next record links to the last one
   * that landed.
   */
  write(record: Record<string, unknown>): void {
    let line: string;
    let digest: string | null = null;
    if (this.chain !== "none") {
      ({ line, digest } = chainLine(record, this.seq + 1, this.prevHash, this.key));
    } else {
      line = serialize(record);
    }
    // Synchronous, and per record. A process killed between the write and a
    // deferred flush would have performed the control call and lost the only
    // record of it, which is the failure this file exists to prevent.
    this.file?.write(Buffer.from(line + "\n", "ascii"));
    for (const sink of this.sinks) sink.write(line);
    if (digest !== null) {
      this.seq += 1;
      this.prevHash = digest;
    }
    // stdout is reserved for the MCP stdio JSON-RPC transport; `index.ts`
    // reassigns console.log to console.error for the same reason.
    console.error(line);
  }

  close(): void {
    this.file?.close();
    for (const sink of this.sinks) sink.close();
  }
}

/** One line for the startup log, so the sink in force is never a guess. */
export function describeAudit(sink: AuditSink): string {
  if (!sink.path) return "audit=stderr only";
  return `audit=${sink.path} fsync=${sink.fsync} chain=${sink.chain}`;
}

// --- the verifier ------------------------------------------------------------

/** What `--verify-audit` found. `problems` make it fail; `notices` do not. */
export interface Verdict {
  records: number;
  chained: number;
  firstSeq: number | null;
  lastSeq: number | null;
  problems: string[];
  notices: string[];
  ok: boolean;
}

/** Split on LF and drop a CR before it.
 *
 * The server writes LF only, but a file that passed through a Windows tool may
 * come back CRLF, and the terminator was never part of what the hash covers — so
 * a converted file still verifies, and every byte of every record is still
 * checked. `audit.py` splits the same way.
 */
function splitLines(content: Buffer): Buffer[] {
  const lines: Buffer[] = [];
  const push = (end: number, start: number) =>
    lines.push(content.subarray(start, end > start && content[end - 1] === 0x0d ? end - 1 : end));
  let start = 0;
  for (let index = 0; index < content.length; index += 1) {
    if (content[index] === 0x0a) {
      push(index, start);
      start = index + 1;
    }
  }
  if (start < content.length) push(content.length, start);
  return lines;
}

/** Check the hash chain across `files`, oldest first, as one stream.
 *
 * Detects, within the documented threat model: a modified record (its hash no
 * longer matches), a deleted, inserted or reordered one (the next record's
 * `prev_hash` or `seq` no longer follows), and an unchained line in the middle of
 * a chain. It cannot detect the *tail* being cut off, nor the head of a stream
 * whose earlier files are not given — both are reported as what they look like,
 * which is a chain that ends, or one that begins at seq > 1.
 */
export function verifyChain(files: Array<[string, Buffer]>, key: Buffer | null): Verdict {
  const verdict: Verdict = {
    records: 0,
    chained: 0,
    firstSeq: null,
    lastSeq: null,
    problems: [],
    notices: [],
    ok: false,
  };
  let prev: { seq: number; hash: string; where: string } | null = null;
  let leading = 0;
  for (const [name, content] of files) {
    // A server that starts on a new (or rotated-to) file begins a chain there, so
    // a restart is only unremarkable as the first record of a file.
    let firstInFile = true;
    splitLines(content).forEach((raw, index) => {
      // Latin-1 keeps one character per byte, so a regex offset is a byte offset.
      const text = raw.toString("latin1");
      if (!text.trim()) return;
      const where = `${name}:${index + 1}`;
      const first = firstInFile;
      firstInFile = false;
      verdict.records += 1;
      let record: Record<string, unknown>;
      try {
        const parsed: unknown = JSON.parse(raw.toString("utf8"));
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("not an object");
        }
        record = parsed as Record<string, unknown>;
      } catch {
        verdict.problems.push(`${where}: not a JSON record (a torn or edited line)`);
        return;
      }
      if (!("hash" in record)) {
        if (prev === null) {
          leading += 1;
        } else {
          verdict.problems.push(
            `${where}: no hash in the middle of a chain (inserted, or written with the chain off)`
          );
        }
        return;
      }
      const trailer = HASH_TRAILER.exec(text);
      const seq = record.seq;
      const prevHash = record.prev_hash;
      if (
        trailer === null ||
        !Number.isInteger(seq) ||
        (seq as number) < 1 ||
        !(prevHash === null || typeof prevHash === "string")
      ) {
        verdict.problems.push(`${where}: malformed chain fields`);
        return;
      }
      const stated = trailer[1];
      const n = seq as number;
      verdict.chained += 1;
      if (verdict.firstSeq === null) verdict.firstSeq = n;
      verdict.lastSeq = n;
      const body = Buffer.concat([raw.subarray(0, trailer.index), Buffer.from("}")]);
      const expected = Buffer.from(digestOf(body, key), "ascii");
      if (!timingSafeEqual(expected, Buffer.from(stated, "ascii"))) {
        const hint = key !== null ? "" : "; pass --key-file if it was written with hmac";
        verdict.problems.push(`${where}: hash mismatch — the record was modified${hint}`);
      }
      if (n === 1 && prevHash === null) {
        if (prev !== null && first) {
          verdict.notices.push(
            `${where}: chain restarts at seq 1; records before it are not linked to records after it`
          );
        } else if (prev !== null) {
          verdict.problems.push(
            `${where}: chain restarts at seq 1 in the middle of a file — records before it ` +
              "were rewritten or the chain was restarted by hand"
          );
        }
      } else if (prev === null) {
        verdict.notices.push(
          `${where}: chain begins at seq ${n}; earlier records are not in the files given`
        );
      } else if (prevHash !== prev.hash || n !== prev.seq + 1) {
        verdict.problems.push(
          `${where}: seq ${n} does not follow seq ${prev.seq} at ${prev.where} — records ` +
            "were deleted, inserted or reordered"
        );
      }
      prev = { seq: n, hash: stated, where };
    });
  }
  if (leading) verdict.notices.push(`${leading} unchained record(s) before the chain begins`);
  if (verdict.chained === 0) verdict.problems.push("no chained records found");
  verdict.ok = verdict.problems.length === 0;
  return verdict;
}

export function describeVerdict(verdict: Verdict): string[] {
  const lines = [
    ...verdict.problems.map((problem) => `PROBLEM ${problem}`),
    ...verdict.notices.map((notice) => `note ${notice}`),
  ];
  let summary = `${verdict.chained} chained record(s)`;
  if (verdict.firstSeq !== null) summary += `, seq ${verdict.firstSeq}..${verdict.lastSeq}`;
  lines.push((verdict.ok ? "OK: " : "FAILED: ") + summary);
  return lines;
}

export const VERIFY_USAGE =
  "usage: opcua-mcp-server --verify-audit FILE [FILE ...] [--key-file KEY]";

/** `--verify-audit`: 0 when the chain holds, 1 when it does not, 2 on misuse. */
export function runVerify(
  argv: string[],
  io: { log: (msg: string) => void; err: (msg: string) => void }
): number {
  const files: string[] = [];
  let keyFile: string | null = null;
  const args = [...argv];
  while (args.length > 0) {
    const arg = args.shift() as string;
    if (arg === "--key-file" || arg.startsWith("--key-file=")) {
      const value = arg.includes("=") ? arg.slice(arg.indexOf("=") + 1) : (args.shift() ?? "");
      if (!value) {
        io.err(`--key-file needs a path\n${VERIFY_USAGE}`);
        return 2;
      }
      keyFile = value;
    } else if (arg.startsWith("-")) {
      io.err(`unknown argument: ${arg}\n${VERIFY_USAGE}`);
      return 2;
    } else {
      files.push(arg);
    }
  }
  if (files.length === 0) {
    io.err(VERIFY_USAGE);
    return 2;
  }
  let key: Buffer | null;
  const contents: Array<[string, Buffer]> = [];
  try {
    key = keyFile ? loadChainKey(keyFile) : null;
    for (const path of files) contents.push([path, readFileSync(path)]);
  } catch (error) {
    io.err(`Error: ${describeError(error)}`);
    return 2;
  }
  const verdict = verifyChain(contents, key);
  for (const line of describeVerdict(verdict)) io.log(line);
  return verdict.ok ? 0 : 1;
}
