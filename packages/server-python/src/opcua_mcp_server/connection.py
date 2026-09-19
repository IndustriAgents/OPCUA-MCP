"""Owns the OPC UA client connection, and puts it back when it breaks.

The connection is expected to break. A plant network drops, an OPC UA server is
restarted for maintenance, a switch reboots — and an MCP server that needed
restarting after any of those would be useless to leave running.

python-opcua has no reconnection of its own: a ``Client`` whose socket has gone
raises on every subsequent call, forever. So recovery is this module's whole job,
and it has two halves:

  * :meth:`OpcuaConnection.reconnect` throws the client away and opens a new one,
    retrying with exponential backoff on the settings from ``config.py`` — the
    same settings, under the same names, that the Node server hands to
    node-opcua's ``connectionStrategy``.
  * :meth:`OpcuaConnection.run` runs one operation and, if it fails *because the
    connection is gone* rather than because the request was wrong, rebuilds the
    connection and (for an idempotent caller) tries once more.

Everything here blocks: python-opcua is synchronous, so the server calls into it
through ``asyncio.to_thread``. Concurrency is therefore real, and the shape of it
matters more than it looks.

**One rebuild at a time, and the backoff outside the lock.** Two tool calls may
reach for the connection at once and must not both build one — so ``reconnect``
*claims* the attempt under the lock, and then does everything else without it.
The earlier version held the lock across the whole retry loop, sleeps and all,
which meant every concurrent call waited out the full budget (7s by default, 32s
with ``OPCUA_RECONNECT_MAX_RETRY=-1``) before it was even told the server was
down. Serialising the callers was right; making them sit through the sleep was
not, and the two are separable (issue #111).

A caller that arrives mid-rebuild waits for *that* attempt and takes its answer,
success or failure. It does not queue a second attempt of its own: every caller
here wants the same session, N threads each running the full backoff is N times
the load on a server that is already struggling, and the last of them would wait
N budgets to be told what the first one already knew. ``connectPromise`` in
``connection.ts`` is the same idea in the shape JavaScript gives it.

``connection.ts`` in the Node server is the other implementation of this
contract. The two differ in how much the client library does for them —
node-opcua repairs its own channel — but they recover from the same failures,
retry on the same errors, and log the same events.
"""

from __future__ import annotations

import secrets
import sys
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from opcua import Client, ua

from .config import ReconnectConfig, reconnect_config, reconnect_delays
from .contract import CONTRACT, NAMESPACE_ARRAY_NODE_ID
from .errors import message
from .policy import tool_policy
from .security import create_client, describe_security, security_config, security_warnings
from .transport_limits import install_receive_guard

T = TypeVar("T")

# Bound what a server may send before any client exists. python-opcua reassembles
# a chunked message into a list with nothing counting it, so a server that never
# terminates the message exhausts this process (CVE-2022-25304, no fixed version,
# unmaintained library). See transport_limits.py for what the guard does and why
# it is a patch.
install_receive_guard()

_DEAD_SESSION = CONTRACT["deadSession"]

#: The OPC UA status codes that mean "the session is gone", by name.
#:
#: The contract names them and python-opcua supplies the numbers, so this list
#: is not a transcription of anything. A name the library stops publishing raises
#: here, at import, rather than quietly never matching again — which was the real
#: risk in the hand-written list this replaces, and the one its own test could
#: not see because it parametrised over the same constant.
DEAD_SESSION_STATUS_CODES: dict[str, int] = {
    name: getattr(ua.StatusCodes, name) for name in _DEAD_SESSION["statusCodeNames"]["names"]
}

#: Failures below OPC UA, which have no status code to carry. Fixed by errno, so
#: matching them in text is as stable as matching a code.
SOCKET_ERROR_CODES: tuple[str, ...] = tuple(_DEAD_SESSION["socketErrors"]["codes"])

#: The remainder, matched in the error's rendered text. The only fragile part of
#: this, and deliberately the smallest: see the contract's own note.
DEAD_SESSION_PHRASES: tuple[str, ...] = tuple(_DEAD_SESSION["phrases"]["texts"])

#: The same codes as a set, for the one place that asks "is this number one of
#: them" on every failure.
_DEAD_SESSION_CODES = frozenset(DEAD_SESSION_STATUS_CODES.values())

#: Everything matched in text, in one tuple, because two of the three groups are
#: and a caller checking "would this be retried" should not have to know which.
DEAD_SESSION_MARKERS: tuple[str, ...] = (
    tuple(DEAD_SESSION_STATUS_CODES) + DEAD_SESSION_PHRASES + SOCKET_ERROR_CODES
)

#: Socket and timeout failures carry no OPC UA status code, so they are
#: recognised by type instead. ``ConnectionError`` and ``TimeoutError`` are both
#: ``OSError`` subclasses; naming them documents what is meant, and the bare
#: ``OSError`` catches what python-opcua raises from a socket it has already
#: closed underneath us (``[Errno 9] Bad file descriptor`` and friends), which
#: carries no more specific type. Erring wide costs at most one needless
#: reconnection; erring narrow leaves the server dead until it is restarted.
_DEAD_SESSION_TYPES = (ConnectionError, TimeoutError, OSError, EOFError)


def is_connection_error(error: BaseException) -> bool:
    """True when ``error`` says the connection died rather than the request being wrong.

    Three kinds of evidence, strongest first.

    The **status code**, where the error carries one. python-opcua raises
    ``UaStatusCodeError`` with a numeric ``code``, and a number cannot be
    reworded by a release note. This is the only check that is not string
    matching, and it is the one that catches the case that matters most.

    The **type**, for a socket or timeout failure that never reached OPC UA at
    all and so has no code to carry.

    The **text**, last, for everything that arrives as prose — which by the time
    an error reaches the dispatcher is most of it, because each tool body
    re-raises as ``ToolError("Failed to read node ns=2;i=3: …")``. The cause
    chain is walked for exactly that reason, so a rewrapped status error is still
    found by its code rather than by its wording.

    The Node server's ``isConnectionError`` answers the same question about the
    same failures, so a retry that happens on one runtime happens on the other.
    """
    if isinstance(error, ua.UaStatusCodeError) and error.code in _DEAD_SESSION_CODES:
        return True
    if isinstance(error, _DEAD_SESSION_TYPES):
        return True
    text = str(error)
    if any(marker in text for marker in DEAD_SESSION_MARKERS):
        return True
    cause = error.__cause__ or error.__context__
    return cause is not None and cause is not error and is_connection_error(cause)


class _Rebuild:
    """One attempt to replace the connection, and what came of it.

    Per-attempt rather than kept on the connection, so a caller waiting on a
    rebuild reads *that* rebuild's outcome. Shared fields would let a second
    attempt, started the moment the first released its claim, overwrite the
    answer a waiter had not yet read.
    """

    __slots__ = ("client", "done", "failure")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.client: Client | None = None
        self.failure: BaseException | None = None


class OpcuaConnection:
    """The OPC UA client this server talks through, and its recovery."""

    def __init__(self, url: str, config: ReconnectConfig | None = None) -> None:
        self._url = url
        self._config = config or reconnect_config()
        #: Guards ``_client`` and ``_rebuilding``, and nothing else. Deliberately
        #: a plain ``Lock``: it is never held across a network call or a sleep, so
        #: there is nothing re-entrant left to support, and a plain lock is one
        #: that cannot accidentally be held twice by a path that grew a caller.
        self._lock = threading.Lock()
        self._client: Client | None = None
        self._last_error: str | None = None
        #: An id for the session currently held; see :attr:`session_id`.
        self._session: str | None = None
        #: The rebuild in flight, so concurrent callers join it rather than start
        #: a second. ``None`` when nothing is being rebuilt.
        self._rebuilding: _Rebuild | None = None
        #: Called with a *new* client after a dead one was replaced, so that
        #: whatever was bound to the old session can be re-established.
        self.on_client_replaced: Callable[[Client], None] | None = None

    @property
    def url(self) -> str:
        """The endpoint this server is configured to talk to."""
        return self._url

    @property
    def client(self) -> Client | None:
        """The connected client, or None while there is no connection."""
        return self._client

    @property
    def connected(self) -> bool:
        """True while this server holds a session it believes is live."""
        return self._client is not None

    @property
    def last_error(self) -> str | None:
        """Why the connection is not up, in the client library's words."""
        return self._last_error

    @property
    def session_id(self) -> str | None:
        """An id for the session this server holds, or None while it holds none.

        Not the OPC UA server's own SessionId. python-opcua discards it and
        node-opcua exposes it, so a field built from it could not mean the same
        thing on both runtimes — and the audit trail needs a field that does.
        This is minted here when a session is established, which is what lets a
        record answer "which of this process's sessions did the call ride on",
        and so lets two writes either side of an outage be told apart.

        It is also what stops a burst of concurrent failures each rebuilding the
        connection in turn: a caller passes the session its operation died on to
        :meth:`reconnect`, and one that has already been replaced needs no second
        rebuild.
        """
        return self._session

    def _open_with_backoff(self) -> Client:
        """One connection, retried with backoff. Raises the last error if none succeeds.

        Holds no lock. The sleeps here are the whole configured budget, and a
        caller blocked behind them learns nothing it could not have been told at
        once — see the module docstring.
        """
        config = security_config()
        # Log to stderr: stdout is reserved for the MCP stdio JSON-RPC transport.
        for warning in security_warnings(config):
            print(f"WARNING: {warning}", file=sys.stderr)

        delays = reconnect_delays(self._config)
        last: BaseException | None = None
        for attempt in range(len(delays) + 1):
            try:
                client = self._open()
            except Exception as error:
                last = error
                self._last_error = str(error)
                if attempt < len(delays):
                    delay = delays[attempt]
                    print(
                        f"OPC UA reconnect: attempt {attempt + 1} failed "
                        f"({error!s}), next try in {delay:g}ms",
                        file=sys.stderr,
                    )
                    time.sleep(delay / 1000)
                continue
            print(f"Connected to OPC UA server ({describe_security(config)})", file=sys.stderr)
            return client

        print(f"Failed to connect to OPC UA server: {last!s}", file=sys.stderr)
        raise last if last is not None else RuntimeError("No OPC UA session available")

    def _open(self) -> Client:
        """One connection attempt, from a client built fresh for it.

        Fresh rather than reused: for a secured connection ``create_client``
        fetches the server's certificate from its endpoint list, and a server
        that has been restarted may be presenting a new one.
        """
        client = create_client(self._url)
        # python-opcua sizes its keep-alive thread from these (0.7 of the smaller
        # of the two), so this is also what decides how quickly an idle
        # connection notices that the server is gone. Its own default is an hour.
        timeout = self._config.session_timeout_ms
        client.session_timeout = timeout
        client.secure_channel_timeout = timeout
        client.connect()

        # Read the NamespaceArray and hand it to the policy, every connect.
        #
        # A namespace *index* is assigned per session, so an allowlist pinned by
        # namespace URI (``nsu=…;i=5``) can only be resolved once the server has
        # said what its namespaces are — and a server that restarted may have
        # loaded them in a different order, which is the whole reason that form
        # exists. One read of one mandatory node; failing it is not fatal, but it
        # does leave URI-pinned entries unresolved, and the policy denies those.
        self._bind_policy_namespaces(client)
        return client

    @staticmethod
    def _bind_policy_namespaces(client: Client) -> None:
        """Tell the tool policy which namespace URI is at which index here."""
        try:
            uris = client.get_node(NAMESPACE_ARRAY_NODE_ID).get_value()
            tool_policy().bind_namespaces([str(uri) for uri in (uris or [])])
        except Exception as error:
            print(
                "WARNING: could not read the server's NamespaceArray, so policy entries "
                f"written as nsu=<uri>;… cannot be resolved and will be denied: {error}",
                file=sys.stderr,
            )

    def disconnect(self) -> None:
        """Drop the connection, quietly. Shared by shutdown and rebuild."""
        with self._lock:
            client = self._client
            self._client = None
            self._session = None
            if client is None:
                return
            try:
                client.disconnect()
                print("Disconnected from OPC UA server", file=sys.stderr)
            except Exception as error:
                print(f"Error disconnecting from OPC UA server: {error}", file=sys.stderr)

    def ensure_connected(self) -> Client:
        """A live client, connecting if there is not one yet.

        Goes through :meth:`reconnect` even for the very first connection, so
        that whatever holds a client is bound to it by exactly one path — the
        first session and the fiftieth arrive the same way.
        """
        with self._lock:
            if self._client is not None:
                return self._client
        return self.reconnect()

    def reconnect(self, stale: str | None = None) -> Client:
        """Throw the client away and build a new one, whatever state it was in.

        The client that comes back holds a *new* session, so anything bound to
        the old one — the subscriptions, above all — is told through
        ``on_client_replaced``.

        ``stale`` is the :attr:`session_id` the caller's operation died on. If
        the connection has already moved past it, the caller's need is met and
        the live client is handed back: rebuilding again would tear down a
        session that is working and re-attach every subscription on it for
        nothing. Without this, a burst of concurrent failures — which is what an
        outage looks like from a server serving several calls — rebuilt once per
        caller in turn.

        One rebuild at a time otherwise. The lock is held only long enough to
        *claim* the attempt; the teardown, the backoff and the re-establishing
        all happen without it, so a concurrent caller is not blocked behind a
        sleep it cannot learn anything from. A caller that arrives mid-rebuild
        takes that rebuild's answer rather than starting a second (issue #111).
        """
        with self._lock:
            if stale is not None and self._client is not None and self._session != stale:
                return self._client
            mine = self._rebuilding is None
            if mine:
                self._rebuilding = _Rebuild()
            attempt = self._rebuilding

        if not mine:
            attempt.done.wait()
            if attempt.client is not None:
                return attempt.client
            # The attempt we waited on failed, and its failure is the answer.
            # Queueing another here would have every waiter run the whole budget
            # in turn, so the last of them waits N budgets to be told what the
            # first one already knew.
            raise (
                attempt.failure
                if attempt.failure is not None
                else RuntimeError("No OPC UA session available")
            )

        try:
            attempt.client = self._open_new_session()
            return attempt.client
        except BaseException as error:
            attempt.failure = error
            raise
        finally:
            with self._lock:
                self._rebuilding = None
            # After the claim is released, so a caller that wakes and finds no
            # client is free to start its own attempt.
            attempt.done.set()

    def _open_new_session(self) -> Client:
        """Drop the old client, open a new one, and rebind what held the old.

        The caller has already claimed the attempt, so this is the only thread
        running it — which is what lets it take no lock while it waits on the
        network.
        """
        self.disconnect()
        client = self._open_with_backoff()
        with self._lock:
            self._client = client
            self._session = secrets.token_hex(8)
            self._last_error = None
        if self.on_client_replaced is not None:
            try:
                self.on_client_replaced(client)
            except Exception as error:
                # Re-establishing what was being monitored must not turn a
                # recovered connection back into a failed tool call.
                print(
                    f"Error re-establishing state on the new OPC UA session: {error}",
                    file=sys.stderr,
                )
        return client

    def run(self, operation: Callable[[], T], may_repeat: bool = True) -> T:
        """Run ``operation`` on a live connection, once more if the session dies.

        The retry exists because a connection can die between the check and the
        call: being connected a moment ago is all anything can ever know. Whether
        running the operation again is *safe* is not this module's to judge — the
        caller says so with ``may_repeat``, and a caller that says no still gets
        the connection rebuilt, so the next call finds a live session.

        The tool dispatcher no longer comes through here. It has to re-authorize
        between the two attempts — the fresh session may have renumbered the
        namespaces the first attempt was authorized against (issue #105) — and it
        reads the contract's own ``retryPolicy`` to decide what may follow a dead
        session at all (issue #106), neither of which belongs in this module.
        What is left is ``get_server_status``, whose whole job is to reach for
        the connection and report what it found.
        """
        self.ensure_connected()
        session = self.session_id
        try:
            return operation()
        except Exception as error:
            if not is_connection_error(error):
                raise
            suffix = " and retrying once" if may_repeat else ""
            print(
                f"OPC UA call failed on a dead session; reconnecting{suffix}",
                file=sys.stderr,
            )
            self.reconnect(stale=session)
            if not may_repeat:
                raise
            return operation()


def not_connected_message(url: str, reason: str) -> str:
    """The message both runtimes give when a tool cannot be served at all.

    It names the endpoint, because the commonest cause is pointing at the wrong
    one, and it names `get_server_status`, because that is the one tool that
    still answers while the connection is down.
    """
    return message("notConnected", url=url, reason=reason)


def describe_error(error: BaseException) -> str:
    """The message to report for a failed connection, in the library's words."""
    text = str(error)
    return text or type(error).__name__


__all__ = [
    "DEAD_SESSION_MARKERS",
    "DEAD_SESSION_PHRASES",
    "DEAD_SESSION_STATUS_CODES",
    "SOCKET_ERROR_CODES",
    "OpcuaConnection",
    "describe_error",
    "is_connection_error",
    "not_connected_message",
]
