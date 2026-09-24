"""Where the control audit trail goes, and what it has to carry (#113, #146).

``audit.ts`` is the Node half, and the two must produce byte-identical records
for the same call: a trail that carries a field on one server and not the other
cannot be read by one tool. ``tests/fixtures/audit.json`` is the table both
runtimes' unit tests are driven from — the record layout, the configuration,
the file-safety rules, the hash chain and the verifier's verdicts.

Every ``control`` and ``alarm-action`` call writes a line here. Reads never do —
a trail that also recorded every read would bury the four lines anyone is ever
looking for.

**What a record says about who.** Four different things, kept apart because
running them together is how a record comes to claim more than anything checked
(#146): ``operator_label`` is what the deployment *configured*
(``OPCUA_OPERATOR_ID``), never verified; ``process_identity`` is the OS account
this process runs as; ``opcua_user_identity`` is the identity token this server
presents to the OPC UA server — its type and, for a username, the name, never a
secret; and ``mcp_principal`` is reserved for a verified remote caller, which a
stdio transport does not have, so it is always null today. ``operator`` is the
schema-1 name for ``operator_label`` and is still written, so existing readers
keep working.

**Where it goes.** stderr always, because things already read it; and with
``OPCUA_AUDIT_FILE``, an append-only JSON-lines file beside it. The file is the
durable copy, and the part this module is careful about:

* **Who can touch it.** Created ``0600``. An existing target that is a symlink,
  not a regular file, owned by another account, or writable by group or others
  stops the server — each is a way for someone else to edit or redirect the
  trail. On Windows the mode bits and owner are not checked (see
  ``SECURITY.md``).
* **When it is on disk.** ``OPCUA_AUDIT_FSYNC=always`` (the default) ``fsync``\\ s
  each record before the call proceeds, so an ``allowed`` line survives a power
  cut, not only a crash. ``none`` stops at the OS page cache.
* **Rotation.** Before every record the path is checked against the open file.
  If a rotator renamed it away, the next record goes to a freshly created and
  freshly validated file at the path — never to whatever was put there if it
  fails the same checks as at startup.
* **Tamper evidence.** ``OPCUA_AUDIT_CHAIN`` adds ``seq``, ``prev_hash`` and
  ``hash`` to each record: a SHA-256 chain, or HMAC-SHA256 with a key from
  ``OPCUA_AUDIT_CHAIN_KEY_FILE``. ``--verify-audit`` checks it. What it can and
  cannot detect is in ``SECURITY.md``, and it is less than "tamper-proof".

**When it cannot be written.** :class:`AuditWriteError`, and the caller fails the
control call *closed*: an ``allowed`` record that did not land means the call is
refused before anything is sent. A record that fails *after* the plant was
touched cannot un-touch it, so that one is reported on stderr and the result is
returned as it was — reporting a write that happened as a failure invites the
model to send it again.

What a record never carries is the *value* being written. A setpoint is process
data, this stream is shown to a user and shipped off the machine, and a test
asserts it on both runtimes.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

#: Where the durable copy goes, or unset for stderr only.
AUDIT_FILE_ENV = "OPCUA_AUDIT_FILE"

#: Who this server is acting for, as the deployment wants it recorded.
OPERATOR_ENV = "OPCUA_OPERATOR_ID"

#: Whether each file record is fsync'd before the call proceeds.
AUDIT_FSYNC_ENV = "OPCUA_AUDIT_FSYNC"

#: Whether, and how, records are hash-chained.
AUDIT_CHAIN_ENV = "OPCUA_AUDIT_CHAIN"

#: The HMAC key for ``OPCUA_AUDIT_CHAIN=hmac-sha256``.
AUDIT_CHAIN_KEY_FILE_ENV = "OPCUA_AUDIT_CHAIN_KEY_FILE"

#: Bumped from the implicit 1 of #113 when the actor fields were split (#146).
SCHEMA_VERSION = 2

FSYNC_MODES = ("always", "none")
CHAIN_MODES = ("none", "sha256", "hmac-sha256")

#: Shorter than this and an HMAC key is guessable rather than secret.
MIN_KEY_BYTES = 32

#: How much of an existing file's tail is read to continue its chain. One record
#: is a few hundred bytes; this is room for a long refusal reason and a torn line.
_TAIL_BYTES = 64 * 1024

#: The trailer a chained line ends with. Matched on the raw bytes, which is what
#: lets either runtime verify a line the other wrote without agreeing on a JSON
#: canonicalization: the hash is over exactly the bytes before it.
_HASH_TRAILER = re.compile(rb',"hash":"([0-9a-f]{64})"\}$')

#: What ``bytes.strip`` and ``audit.ts`` both treat as surrounding whitespace.
_KEY_WHITESPACE = b" \t\n\r\x0b\x0c"


class AuditWriteError(RuntimeError):
    """A record could not be made durable. Control calls fail closed on this.

    Not a ``ValueError``: the dispatcher catches that as a refusal of the call's
    *arguments*, and this is a refusal of the call's *record*.
    """


def operator_id(env: Mapping[str, str] | None = None) -> str | None:
    """The operator label to stamp on every record, or None if unset.

    A label rather than an identity: this server has no notion of *who* is
    calling — one process, one configured endpoint, whoever holds the MCP client
    — and pretending otherwise would put a name on a record that nothing
    verified. What it can honestly say is which deployment the record came from,
    which is what a reviewer correlating several servers needs.
    """
    raw = (env if env is not None else os.environ).get(OPERATOR_ENV, "").strip()
    return raw or None


# --- configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class AuditConfig:
    """What ``OPCUA_AUDIT_*`` asked for, validated but with nothing opened yet."""

    file: str | None = None
    fsync: str = "always"
    chain: str = "none"
    key_file: str | None = None


def parse_audit_config(env: Mapping[str, str] | None = None) -> AuditConfig:
    """Read and cross-check the ``OPCUA_AUDIT_*`` variables. Raises ValueError."""
    env = env if env is not None else os.environ

    def read(name: str) -> str | None:
        value = env.get(name, "").strip()
        return value or None

    def choice(name: str, allowed: tuple[str, ...], default: str) -> str:
        raw = read(name)
        if raw is None:
            return default
        if raw.lower() not in allowed:
            raise ValueError(f"{name} must be one of {', '.join(allowed)}; got {raw}")
        return raw.lower()

    path = read(AUDIT_FILE_ENV)
    fsync = choice(AUDIT_FSYNC_ENV, FSYNC_MODES, "always")
    chain = choice(AUDIT_CHAIN_ENV, CHAIN_MODES, "none")
    key_file = read(AUDIT_CHAIN_KEY_FILE_ENV)

    # Refused rather than ignored, each of them: a protection that is configured
    # and silently not in force reads, in a config file, exactly like one that is.
    if chain != "none" and path is None:
        raise ValueError(
            f"{AUDIT_CHAIN_ENV}={chain} requires {AUDIT_FILE_ENV}: the chain is kept in "
            "the file it protects"
        )
    if chain == "hmac-sha256" and key_file is None:
        raise ValueError(f"{AUDIT_CHAIN_ENV}=hmac-sha256 requires {AUDIT_CHAIN_KEY_FILE_ENV}")
    if key_file is not None and chain != "hmac-sha256":
        raise ValueError(
            f"{AUDIT_CHAIN_KEY_FILE_ENV} is only used with {AUDIT_CHAIN_ENV}=hmac-sha256; "
            f"got {AUDIT_CHAIN_ENV}={chain}"
        )
    return AuditConfig(file=path, fsync=fsync, chain=chain, key_file=key_file)


def load_chain_key(path: str) -> bytes:
    """The HMAC key in ``path``, with surrounding whitespace dropped.

    Held to the rules a private key is: a key the rest of the machine can read is
    a key anyone on it can forge a chain with. Surrounding whitespace is dropped
    because ``openssl rand -hex 32 > key`` ends in a newline, and a key that
    silently included it would verify nowhere else.
    """
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{AUDIT_CHAIN_KEY_FILE_ENV} {path} is not a regular file")
        if os.name == "posix" and info.st_mode & 0o077:
            raise ValueError(
                f"{AUDIT_CHAIN_KEY_FILE_ENV} {path} is readable or writable by group or others "
                f"(mode {_octal(info.st_mode)}); chmod 600 it"
            )
        with open(path, "rb") as handle:
            key = handle.read().strip(_KEY_WHITESPACE)
    except OSError as error:
        raise ValueError(f"Cannot read {AUDIT_CHAIN_KEY_FILE_ENV} {path}: {error}") from error
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(
            f"{AUDIT_CHAIN_KEY_FILE_ENV} {path} holds {len(key)} bytes; at least "
            f"{MIN_KEY_BYTES} are required (e.g. openssl rand -hex 32)"
        )
    return key


# --- the record ------------------------------------------------------------------


def process_identity() -> dict[str, Any]:
    """The OS account this process runs as, and which process it is.

    The effective uid and the name the account database gives it — not
    ``$USER``, which is whatever the launching environment said. Windows has no
    uid; there the name is the best the platform offers, and is best effort.
    """
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    user: str | None
    try:
        if uid is not None:
            import pwd

            user = pwd.getpwuid(uid).pw_name
        else:
            import getpass

            user = getpass.getuser()
    except Exception:
        user = None
    return {"uid": uid, "user": user, "pid": os.getpid()}


def certificate_sha256(path: str) -> str | None:
    """The SHA-256 fingerprint of a PEM or DER certificate, or None if unreadable.

    The DER bytes, as every certificate tool computes a fingerprint. Parsed by
    hand rather than through a crypto library so the Node half can do the same
    in the same few lines and get the same answer.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    match = re.search(rb"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", raw, re.S)
    if match:
        try:
            raw = base64.b64decode(b"".join(match.group(1).split()), validate=True)
        except ValueError:
            return None
    return hashlib.sha256(raw).hexdigest()


def opcua_user_identity(username: str | None, user_cert: str | None) -> dict[str, Any]:
    """The identity token this server presents to the OPC UA server, minus secrets.

    What the *plant* was told, which is the one identity in the record that
    something other than this process checked. The password and the private key
    never appear; a username is not a secret and is what the server's own audit
    log will name.
    """
    if user_cert is not None:
        return {
            "type": "certificate",
            "username": None,
            "certificate_sha256": certificate_sha256(user_cert),
        }
    if username is not None:
        return {"type": "username", "username": username, "certificate_sha256": None}
    return {"type": "anonymous", "username": None, "certificate_sha256": None}


def _configured_user_identity() -> dict[str, Any] | None:
    try:
        # Late: `security` imports the OPC UA stack, which `--verify-audit` does
        # not need and should not pay for.
        from .security import security_config

        config = security_config()
    except Exception:
        return None
    return opcua_user_identity(config.username, config.user_cert)


def build_record(
    *,
    timestamp: str,
    call_id: str | None,
    attempt: int,
    endpoint: str | None,
    session: str | None,
    session_generation: int | None,
    operator_label: str | None,
    process_identity: dict[str, Any] | None,
    opcua_user_identity: dict[str, Any] | None,
    profile: str,
    control: str | None,
    tool: str,
    decision: str,
    targets: Mapping[str, Any],
    reason: str | None = None,
) -> dict[str, Any]:
    """One audit record, in the canonical field order.

    The order is part of the record: ``audit.ts`` builds the same keys in the
    same order, ``tests/fixtures/audit.json`` pins it, and the file is read by eye
    as often as by a parser.
    """
    record: dict[str, Any] = {
        "event": "opcua_mcp_policy",
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp,
        # Second after the timestamp, so it can be grepped for to pull one call's
        # whole story out of a shipped log.
        "call_id": call_id,
        # Which physical attempt this line is about: one call can reach the plant
        # twice, and a trail whose purpose is "what reached the plant" counts both.
        "attempt": attempt,
        # Which plant, over which of this process's sessions. A node id is not
        # stable across a server restart, so a target is only interpretable later
        # alongside these.
        "endpoint": endpoint,
        "session": session,
        "session_generation": session_generation,
        # Configured, not verified — see the module docstring.
        "operator_label": operator_label,
        # The schema-1 name, kept so existing readers keep working.
        "operator": operator_label,
        # Reserved for a verified remote identity. A stdio transport has none.
        "mcp_principal": None,
        "process_identity": process_identity,
        "opcua_user_identity": opcua_user_identity,
        "profile": profile,
        # The control gate (#134): `secured`, the lab override in force, or
        # `blocked`.
        "control": control,
        "tool": tool,
        "decision": decision,
        **targets,
    }
    if reason:
        record["reason"] = reason
    return record


def serialize(record: Mapping[str, Any]) -> str:
    """One record as the line both runtimes write for it.

    ASCII-only, which is Python's default and what ``audit.ts`` reproduces: the
    two write the same bytes for the same record, not merely the same JSON.
    """
    return json.dumps(record, separators=(",", ":"))


# --- the hash chain --------------------------------------------------------------


def _digest(body: bytes, key: bytes | None) -> str:
    if key is None:
        return hashlib.sha256(body).hexdigest()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def chain_line(
    record: Mapping[str, Any], seq: int, prev_hash: str | None, key: bytes | None
) -> tuple[str, str]:
    """``record`` with its chain trailer, and the hash that trailer carries.

    The hash covers the serialized record *with* ``seq`` and ``prev_hash`` and
    closed where ``hash`` would start — so it pins the record's content, its
    position, and the record before it, and needs nothing but the raw line to
    recompute.
    """
    body = serialize({**record, "seq": seq, "prev_hash": prev_hash})
    digest = _digest(body.encode("ascii"), key)
    return f'{body[:-1]},"hash":"{digest}"}}', digest


def _last_chain_link(raw_tail: bytes) -> tuple[int, str] | None:
    """The ``(seq, hash)`` a restarted server continues from, if the file has one.

    The last line that parses; a torn one after it (a crash mid-write) is
    skipped rather than chained onto, and ``--verify-audit`` reports it. A last
    record with no chain fields means the file was written unchained, and the
    chain starts afresh.
    """
    for line in reversed(raw_tail.split(b"\n")):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        seq, digest = record.get("seq"), record.get("hash")
        if isinstance(seq, int) and not isinstance(seq, bool) and isinstance(digest, str):
            return seq, digest
        return None
    return None


# --- the file --------------------------------------------------------------------


def _octal(mode: int) -> str:
    return f"{stat.S_IMODE(mode):04o}"


def unsafe_target_reason(
    path: str, *, kind: str, uid: int | None, mode: int, euid: int | None
) -> str | None:
    """Why an existing audit target must not be written, or None if it may be.

    ``kind`` is ``file``, ``symlink``, ``directory`` or ``other``. ``euid`` is
    None where the platform has no uid, and then neither ownership nor mode is
    checked: on Windows the bits are not what decides access (see SECURITY.md).

    Group- or world-*readable* is allowed on purpose. A collector reading the
    file as its own group (``0640``, as logrotate's ``create`` commonly sets it)
    is a legitimate deployment; a collector that can *write* it can rewrite it.
    """
    if kind == "symlink":
        return f"{AUDIT_FILE_ENV} {path} is a symbolic link; point it at the real file"
    if kind != "file":
        return f"{AUDIT_FILE_ENV} {path} is not a regular file"
    if euid is None:
        return None
    if uid != euid:
        return f"{AUDIT_FILE_ENV} {path} is owned by uid {uid}, not by this process (uid {euid})"
    if mode & 0o022:
        return (
            f"{AUDIT_FILE_ENV} {path} is writable by group or others (mode {_octal(mode)}); "
            "chmod 600 it"
        )
    return None


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def _euid() -> int | None:
    return os.geteuid() if hasattr(os, "geteuid") else None


def _check(path: str, info: os.stat_result) -> None:
    reason = unsafe_target_reason(
        path, kind=_kind(info.st_mode), uid=info.st_uid, mode=info.st_mode, euid=_euid()
    )
    if reason:
        raise AuditWriteError(reason)


def _fsync_directory(path: str) -> None:
    """Make a newly created file's directory entry durable, where that exists.

    Best effort: some filesystems refuse to fsync a directory, and Windows cannot
    open one. The records themselves are fsync'd regardless.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class _AuditFile:
    """The durable copy: validated, append-only, reopened on rotation."""

    def __init__(self, path: str, *, fsync: bool) -> None:
        self.path = path
        self._fsync = fsync
        self._fd: int | None = None
        self._identity: tuple[int, int] | None = None
        #: The file did not end in a newline when opened — a torn last line — so
        #: the next record starts on its own line rather than being glued to it.
        self._needs_newline = False

    def open(self) -> bytes:
        """Open (creating ``0600`` if absent) and return the file's tail."""
        try:
            existed = True
            try:
                _check(self.path, os.lstat(self.path))
            except FileNotFoundError:
                existed = False
            # O_NOFOLLOW closes the gap between the lstat above and this open;
            # O_NONBLOCK stops a FIFO swapped in there from hanging the server
            # until something reads it. Neither exists on Windows.
            flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(self.path, flags, 0o600)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise AuditWriteError(
                        f"{AUDIT_FILE_ENV} {self.path} is a symbolic link; "
                        "point it at the real file"
                    ) from error
                raise
            try:
                info = os.fstat(fd)
                # Again, on what was actually opened: O_CREAT opens an existing
                # file as happily as it creates one, so whatever raced in is
                # checked here rather than trusted.
                _check(self.path, info)
                tail = b""
                if info.st_size > 0:
                    start = max(0, info.st_size - _TAIL_BYTES)
                    tail = self._read_tail(fd, start)
                    self._needs_newline = not tail.endswith(b"\n")
            except BaseException:
                os.close(fd)
                raise
        except AuditWriteError:
            raise
        except OSError as error:
            raise AuditWriteError(f"Cannot open {AUDIT_FILE_ENV} {self.path}: {error}") from error
        if not existed and self._fsync:
            _fsync_directory(self.path)
        self._fd = fd
        self._identity = (info.st_dev, info.st_ino)
        return tail

    @staticmethod
    def _read_tail(fd: int, start: int) -> bytes:
        # Moving the offset is harmless: O_APPEND puts every write at the end
        # regardless of where the last read left it.
        os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        while chunk := os.read(fd, _TAIL_BYTES):
            chunks.append(chunk)
        return b"".join(chunks)

    def _current(self) -> int:
        """The fd to write to, reopening if the path no longer names it."""
        if self._fd is not None:
            try:
                info = os.lstat(self.path)
                moved = (info.st_dev, info.st_ino) != self._identity
            except FileNotFoundError:
                moved = True
            except OSError as error:
                raise AuditWriteError(
                    f"Cannot stat {AUDIT_FILE_ENV} {self.path}: {error}"
                ) from error
            if not moved:
                return self._fd
            # Rotated away, deleted, or replaced. Whatever is at the path now is
            # held to the startup rules; the rotated file is not written again.
            self.close()
        self.open()
        assert self._fd is not None
        return self._fd

    def write(self, data: bytes) -> None:
        fd = self._current()
        if self._needs_newline:
            data = b"\n" + data
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            if self._fsync:
                os.fsync(fd)
        except OSError as error:
            raise AuditWriteError(f"Cannot write {AUDIT_FILE_ENV} {self.path}: {error}") from error
        self._needs_newline = False

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
                self._identity = None


class LineSink(Protocol):
    """Somewhere a finished audit line can go — the external-sink seam.

    A syslog, journald, Windows Event Log or collector sink implements this and is
    handed to :class:`AuditSink`. The contract, which the file sink follows:

    * ``write`` is synchronous and returns only once the line is as durable as
      that sink promises. There is no queue in front of it, so there is nothing
      unbounded to report on; a sink that buffers must bound the buffer itself
      and raise when it is full.
    * ``write`` raises :class:`AuditWriteError` when the line did not land. For a
      pre-dispatch record that refuses the control call.
    * Every sink receives the same bytes — chain trailer included — so any one
      of them can be verified on its own.
    """

    def write(self, line: str) -> None: ...

    def close(self) -> None: ...


class AuditSink:
    """The stderr stream, optionally a durable file, and any further line sinks."""

    def __init__(
        self,
        path: str | None = None,
        *,
        fsync: str = "always",
        chain: str = "none",
        chain_key: bytes | None = None,
        sinks: Sequence[LineSink] = (),
    ) -> None:
        if chain not in CHAIN_MODES:
            raise ValueError(f"{AUDIT_CHAIN_ENV} must be one of {', '.join(CHAIN_MODES)}")
        if chain == "hmac-sha256" and chain_key is None:
            raise ValueError(f"{AUDIT_CHAIN_ENV}=hmac-sha256 requires a key")
        if chain != "none" and not path:
            raise ValueError(f"{AUDIT_CHAIN_ENV}={chain} requires {AUDIT_FILE_ENV}")
        self._path = path
        self._fsync = fsync
        self._chain = chain
        self._key = chain_key if chain == "hmac-sha256" else None
        self._sinks = list(sinks)
        self._lock = threading.Lock()
        self._seq = 0
        self._prev_hash: str | None = None
        self._identity: dict[str, Any] | None = None
        self._file: _AuditFile | None = None
        if path:
            self._file = _AuditFile(path, fsync=fsync == "always")
            try:
                tail = self._file.open()
            except AuditWriteError as error:
                # Fatal, and deliberately so. An operator who set this expects a
                # durable record; falling back to stderr would leave them
                # believing they had one.
                raise ValueError(str(error)) from error
            if chain != "none":
                # A restart continues the file's chain rather than starting a
                # second one in the middle of it.
                link = _last_chain_link(tail)
                if link is not None:
                    self._seq, self._prev_hash = link

    @classmethod
    def from_config(cls, config: AuditConfig) -> AuditSink:
        key = load_chain_key(config.key_file) if config.key_file else None
        return cls(config.file, fsync=config.fsync, chain=config.chain, chain_key=key)

    @property
    def path(self) -> str | None:
        """The file being written beside stderr, or None if there is none."""
        return self._path

    @property
    def fsync(self) -> str:
        return self._fsync

    @property
    def chain(self) -> str:
        return self._chain

    def identity(self) -> dict[str, Any]:
        """``process_identity`` and ``opcua_user_identity``, worked out once."""
        if self._identity is None:
            self._identity = {
                "process_identity": process_identity(),
                "opcua_user_identity": _configured_user_identity(),
            }
        return self._identity

    def write(self, record: dict[str, Any]) -> None:
        """One record: to the file (and any further sinks), then to stderr.

        Raises :class:`AuditWriteError` if a durable sink did not take it. Then
        nothing goes to stderr as a record either — a line there saying
        ``allowed`` for a call that is about to be refused would be the one
        record in the trail that is false — and the chain does not advance, so
        the next record links to the last one that landed.
        """
        with self._lock:
            if self._chain != "none":
                line, digest = chain_line(record, self._seq + 1, self._prev_hash, self._key)
            else:
                line, digest = serialize(record), None
            data = (line + "\n").encode("ascii")
            if self._file is not None:
                self._file.write(data)
            for sink in self._sinks:
                sink.write(line)
            if digest is not None:
                self._seq += 1
                self._prev_hash = digest
            # stdout is reserved for the MCP stdio JSON-RPC transport.
            print(line, file=sys.stderr)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
        for sink in self._sinks:
            sink.close()


def describe_audit(sink: AuditSink) -> str:
    """One line for the startup log, so the sink in force is never a guess."""
    if not sink.path:
        return "audit=stderr only"
    return f"audit={sink.path} fsync={sink.fsync} chain={sink.chain}"


# --- the verifier ----------------------------------------------------------------


@dataclass
class Verdict:
    """What ``--verify-audit`` found. ``problems`` make it fail; ``notices`` do not."""

    records: int = 0
    chained: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    problems: list[str] | None = None
    notices: list[str] | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def verify_chain(files: Sequence[tuple[str, bytes]], key: bytes | None) -> Verdict:
    """Check the hash chain across ``files``, oldest first, as one stream.

    Detects, within the documented threat model: a modified record (its hash no
    longer matches), a deleted, inserted or reordered one (the next record's
    ``prev_hash`` or ``seq`` no longer follows), and an unchained line in the
    middle of a chain. It cannot detect the *tail* being cut off, nor the head of
    a stream whose earlier files are not given — both are reported as what they
    look like, which is a chain that ends, or one that begins at seq > 1.
    """
    verdict = Verdict(problems=[], notices=[])
    assert verdict.problems is not None and verdict.notices is not None
    prev: tuple[int, str, str] | None = None  # (seq, hash, where)
    leading = 0
    for name, content in files:
        lines = content.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        # A server that starts on a new (or rotated-to) file begins a chain there,
        # so a restart is only unremarkable as the first record of a file.
        first_in_file = True
        for number, raw in enumerate(lines, start=1):
            if not raw.strip():
                continue
            where = f"{name}:{number}"
            first, first_in_file = first_in_file, False
            verdict.records += 1
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("not an object")
            except ValueError:
                verdict.problems.append(f"{where}: not a JSON record (a torn or edited line)")
                continue
            if "hash" not in record:
                if prev is None:
                    leading += 1
                else:
                    verdict.problems.append(
                        f"{where}: no hash in the middle of a chain (inserted, or written "
                        "with the chain off)"
                    )
                continue
            trailer = _HASH_TRAILER.search(raw)
            seq, prev_hash = record.get("seq"), record.get("prev_hash")
            if (
                trailer is None
                or not isinstance(seq, int)
                or isinstance(seq, bool)
                or seq < 1
                or not (prev_hash is None or isinstance(prev_hash, str))
            ):
                verdict.problems.append(f"{where}: malformed chain fields")
                continue
            stated = trailer.group(1).decode("ascii")
            verdict.chained += 1
            if verdict.first_seq is None:
                verdict.first_seq = seq
            verdict.last_seq = seq
            body = raw[: trailer.start()] + b"}"
            if not hmac.compare_digest(_digest(body, key), stated):
                hint = "" if key is not None else "; pass --key-file if it was written with hmac"
                verdict.problems.append(f"{where}: hash mismatch — the record was modified{hint}")
            if seq == 1 and prev_hash is None:
                if prev is not None and first:
                    verdict.notices.append(
                        f"{where}: chain restarts at seq 1; records before it are not linked "
                        "to records after it"
                    )
                elif prev is not None:
                    verdict.problems.append(
                        f"{where}: chain restarts at seq 1 in the middle of a file — records "
                        "before it were rewritten or the chain was restarted by hand"
                    )
            elif prev is None:
                verdict.notices.append(
                    f"{where}: chain begins at seq {seq}; earlier records are not in the "
                    "files given"
                )
            elif prev_hash != prev[1] or seq != prev[0] + 1:
                verdict.problems.append(
                    f"{where}: seq {seq} does not follow seq {prev[0]} at {prev[2]} — records "
                    "were deleted, inserted or reordered"
                )
            prev = (seq, stated, where)
    if leading:
        verdict.notices.append(f"{leading} unchained record(s) before the chain begins")
    if verdict.chained == 0:
        verdict.problems.append("no chained records found")
    return verdict


def describe_verdict(verdict: Verdict) -> list[str]:
    lines = [*(f"PROBLEM {p}" for p in verdict.problems or [])]
    lines += [f"note {n}" for n in verdict.notices or []]
    summary = f"{verdict.chained} chained record(s)"
    if verdict.first_seq is not None:
        summary += f", seq {verdict.first_seq}..{verdict.last_seq}"
    lines.append(("OK: " if verdict.ok else "FAILED: ") + summary)
    return lines


VERIFY_USAGE = "usage: opcua-mcp-server --verify-audit FILE [FILE ...] [--key-file KEY]"


def run_verify(argv: Sequence[str]) -> int:
    """``--verify-audit``: 0 when the chain holds, 1 when it does not, 2 on misuse."""
    files: list[str] = []
    key_file: str | None = None
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--key-file" or arg.startswith("--key-file="):
            value = arg.partition("=")[2] if "=" in arg else (args.pop(0) if args else "")
            if not value:
                print(f"--key-file needs a path\n{VERIFY_USAGE}", file=sys.stderr)
                return 2
            key_file = value
        elif arg.startswith("-"):
            print(f"unknown argument: {arg}\n{VERIFY_USAGE}", file=sys.stderr)
            return 2
        else:
            files.append(arg)
    if not files:
        print(VERIFY_USAGE, file=sys.stderr)
        return 2
    try:
        key = load_chain_key(key_file) if key_file else None
        contents = []
        for path in files:
            with open(path, "rb") as handle:
                contents.append((path, handle.read()))
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    verdict = verify_chain(contents, key)
    for line in describe_verdict(verdict):
        print(line)
    return 0 if verdict.ok else 1


__all__ = [
    "AUDIT_CHAIN_ENV",
    "AUDIT_CHAIN_KEY_FILE_ENV",
    "AUDIT_FILE_ENV",
    "AUDIT_FSYNC_ENV",
    "OPERATOR_ENV",
    "SCHEMA_VERSION",
    "AuditConfig",
    "AuditSink",
    "AuditWriteError",
    "LineSink",
    "Verdict",
    "build_record",
    "chain_line",
    "describe_audit",
    "describe_verdict",
    "load_chain_key",
    "opcua_user_identity",
    "operator_id",
    "parse_audit_config",
    "process_identity",
    "run_verify",
    "serialize",
    "unsafe_target_reason",
    "verify_chain",
]
