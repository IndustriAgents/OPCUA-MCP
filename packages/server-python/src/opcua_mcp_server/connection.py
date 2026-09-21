"""Owns the OPC UA client connection, and puts it back when it breaks.

The connection is expected to break. A plant network drops, an OPC UA server is
restarted for maintenance, a switch reboots — and an MCP server that needed
restarting after any of those would be useless to leave running.

python-opcua has no reconnection of its own: a ``Client`` whose socket has gone
raises on every subsequent call, forever. So recovery is this module's whole job,
and it has two halves:

  * :meth:`OpcuaConnection.connect` retries with exponential backoff, on the
    settings from ``config.py`` — the same settings, under the same names, that
    the Node server hands to node-opcua's ``connectionStrategy``.
  * :meth:`OpcuaConnection.run` runs one operation and, if it fails *because the
    connection is gone* rather than because the request was wrong, rebuilds the
    connection and (for an idempotent caller) tries once more.

Everything here blocks: python-opcua is synchronous, so the server calls into it
through ``asyncio.to_thread``. A lock guards the client, because two tool calls
may reach for it at once and must not both build one.

``connection.ts`` in the Node server is the other implementation of this
contract. The two differ in how much the client library does for them —
node-opcua repairs its own channel — but they recover from the same failures,
retry on the same errors, and log the same events.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from opcua import Client

from .config import ReconnectConfig, reconnect_config, reconnect_delays
from .contract import NAMESPACE_ARRAY_NODE_ID
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

#: OPC UA status codes and socket errors that mean "the session is gone".
#:
#: Matched in the *message*, because by the time an error reaches the dispatcher
#: it has usually been rewrapped as prose ("Failed to read node ns=2;i=3: ...").
#: python-opcua renders a status error as ``"<description>"(BadSessionIdInvalid)``,
#: so the code name is in the text. Every entry names a failure of the connection
#: rather than of the request, which is what makes retrying on a fresh session
#: meaningful: a ``BadNodeIdUnknown`` would fail exactly the same way the second
#: time. Kept in step with ``DEAD_SESSION_MARKERS`` in the Node server.
DEAD_SESSION_MARKERS = (
    "BadSessionIdInvalid",
    "BadSessionClosed",
    "BadSessionNotActivated",
    "BadSecureChannelClosed",
    "BadSecureChannelIdInvalid",
    "BadServerNotConnected",
    "BadNotConnected",
    "BadConnectionClosed",
    "BadConnectionRejected",
    "BadDisconnect",
    "BadNoCommunication",
    "BadCommunicationError",
    "BadServerHalted",
    "BadTcpInternalError",
    "No OPC UA session available",
    "socket has been disconnected",
    "The connection has been rejected",
    "ECONNREFUSED",
    "ECONNRESET",
    "EPIPE",
    "ETIMEDOUT",
    "EHOSTUNREACH",
    "ENETUNREACH",
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

    The Node server's ``isConnectionError`` answers the same question about the
    same failures, so a retry that happens on one runtime happens on the other.
    """
    if isinstance(error, _DEAD_SESSION_TYPES):
        return True
    text = str(error)
    if any(marker in text for marker in DEAD_SESSION_MARKERS):
        return True
    cause = error.__cause__ or error.__context__
    return cause is not None and cause is not error and is_connection_error(cause)


class OpcuaConnection:
    """The OPC UA client this server talks through, and its recovery."""

    def __init__(self, url: str, config: ReconnectConfig | None = None) -> None:
        self._url = url
        self._config = config or reconnect_config()
        self._lock = threading.RLock()
        self._client: Client | None = None
        self._last_error: str | None = None
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

    def connect(self) -> Client:
        """Connect, retrying with backoff. Raises the last error if none succeeds."""
        with self._lock:
            if self._client is not None:
                return self._client

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
                self._client = client
                self._last_error = None
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
            if client is None:
                return
            try:
                client.disconnect()
                print("Disconnected from OPC UA server", file=sys.stderr)
            except Exception as error:
                print(f"Error disconnecting from OPC UA server: {error}", file=sys.stderr)

    def ensure_connected(self) -> Client:
        """A live client, connecting if there is not one yet.

        Goes through :meth:`reconnect` rather than :meth:`connect` even for the
        very first connection, so that whatever holds a client is bound to it by
        exactly one path — the first session and the fiftieth arrive the same way.
        """
        with self._lock:
            if self._client is not None:
                return self._client
            return self.reconnect()

    def reconnect(self) -> Client:
        """Throw the client away and build a new one, whatever state it was in.

        The client that comes back holds a *new* session, so anything bound to
        the old one — the subscriptions, above all — is told through
        ``on_client_replaced``.
        """
        with self._lock:
            self.disconnect()
            client = self.connect()
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
            self.reconnect()
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
    "OpcuaConnection",
    "describe_error",
    "is_connection_error",
    "not_connected_message",
]
