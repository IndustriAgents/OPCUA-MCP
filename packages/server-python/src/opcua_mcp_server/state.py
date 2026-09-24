"""Everything one OPC UA MCP server instance owns (issue #116).

The Node runtime keeps its state on objects: `OpcuaConnection`, `OpcuaTools` and
the policy are constructed in `index.ts` and passed down. Python kept the same
state in module globals — the connection, the capability answers, the
subscription and event managers, the per-session node metadata and the audit
sink — which meant the Python server could not be instantiated twice in one
process while the Node one nearly could. The contract pins the tool surface and
the tests pin the semantics; nothing pinned the *structure*, and that is exactly
where the two halves had drifted.

It matters for the pooled-sessions / per-endpoint-policy direction, which needs
one server instance per endpoint. It also removes a subtler hazard that has
nothing to do with multiple endpoints: module state outlives a lifespan, so
anything left behind by a stopped server is still there for the next one.

The docstrings those globals carried argued that they had to be module-level
because ``list_tools`` is handed no ``Context`` and so cannot reach the lifespan
state. That is an argument for putting them on the *server instance* — which
``PolicyMCPServer`` already is — not in the module, and that is what this is.

Reached two ways, and they are the same object:

* ``PolicyMCPServer.state``, for ``list_tools`` and ``call_tool``, which are
  methods and so have ``self``;
* ``ctx.request_context.lifespan_context["state"]`` for the tools, which are
  handed a ``Context`` and no ``self``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from .audit import AuditSink
from .capabilities import CapabilityAnswers
from .config import SERVER_URL, WARM_UP_WAIT_MS
from .connection import OpcuaConnection
from .events import EventSubscriptions
from .node_metadata import NodeMetadata
from .operation_limits import UNSTATED
from .policy import ToolPolicy, tool_policy
from .subscriptions import SubscriptionManager


class ServerState:
    """One server's connection, capability answers, managers and audit sink.

    Constructed before the lifespan runs, because ``list_tools`` may be answered
    before any connection exists. The lifespan fills in :attr:`connection`.
    """

    def __init__(
        self,
        url: str = SERVER_URL,
        policy: ToolPolicy | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.url = url
        self._policy = policy
        #: stderr-only until `main` replaces it. `main` is the one place allowed
        #: to fail on a bad OPCUA_AUDIT_FILE: a server imported as a module (the
        #: tests do) must not need the environment to be right.
        self.audit = audit if audit is not None else AuditSink()

        #: Set by the lifespan, and None before it and after it.
        self.connection: OpcuaConnection | None = None

        #: What the server was found to support, and on which session
        #: generation. Read on every (re)connect and never trusted across a
        #: generation — a restarted server may not be the same server — and
        #: never consulted by ``list_tools`` (#140). Replaced whole, never
        #: mutated; see :class:`~.capabilities.CapabilityAnswers`.
        self.capabilities = CapabilityAnswers()
        #: What the connected server says one service call may carry — see
        #: operation_limits.py. Probed and forgotten with the capabilities.
        self.operation_limits: dict[str, int | None] = dict(UNSTATED)

        self.subscriptions = SubscriptionManager()
        self.events = EventSubscriptions()
        #: What each node published about its own number, for the life of one
        #: session. Dropped on rebind for the same reason the capabilities are.
        self.node_metadata = NodeMetadata()

        #: The startup warm-up, set by the lifespan, and when requests stop
        #: waiting for it (event-loop time). See :meth:`await_warm_up`.
        self.warm_up: asyncio.Future | None = None
        self._warm_up_deadline = 0.0
        #: How long :meth:`await_warm_up` gives the warm-up, from its start. An
        #: attribute so the unit tests can shorten it; the server uses the constant.
        self.warm_up_wait_ms: float = WARM_UP_WAIT_MS

    def start_warm_up(self, warm_up: Awaitable[None]) -> asyncio.Future:
        """Run the warm-up beside the requests rather than before them.

        The lifespan used to await it, and the MCP SDK answers nothing — not
        ``initialize`` — until the lifespan has entered, so a plant that was down
        held the whole protocol back for a full connection round (#136).
        """
        self.warm_up = asyncio.ensure_future(warm_up)
        self._warm_up_deadline = asyncio.get_running_loop().time() + self.warm_up_wait_ms / 1000
        return self.warm_up

    async def await_connection_in_flight(self) -> None:
        """Wait for the warm-up, and any other connection round in flight, unbounded.

        For a tool call, before it is authorized and audited. Both read the
        session: the policy resolves ``nsu=`` allowlist entries through the
        namespace mapping bound on connect, and the audit record names the
        session the call rides on (#105, #107). When the lifespan awaited the
        warm-up, every call found it finished; a call that arrives during it now
        waits for it, where before it would have been refused a URI-pinned node
        and audited against no session at all. Bounded by the round itself,
        which always ends. Never starts a round: a call made while disconnected
        connects after it is authorized, as it always has. The Node server's
        ``OpcuaTools.awaitConnectionInFlight`` is the same wait.
        """
        warm_up = self.warm_up
        if warm_up is not None and not warm_up.done():
            await asyncio.wait({warm_up})
        connection = self.connection
        if connection is not None and connection.connecting:
            await asyncio.to_thread(connection.settle)

    async def await_warm_up(self) -> None:
        """Wait for the startup warm-up, but never past ``warm_up_wait_ms`` from its start.

        For ``get_server_status``, which reports what the server knows about the
        connection. Against a plant that is up the warm-up finishes well inside
        the window, so the first status is a connected one — the reason the
        warm-up used to run before any request was served. Against a plant that
        is down it can take the whole round, and a server that is to be
        diagnosable has to answer before then, from what it knows. ``tools/list``
        does not wait: since #140 there is nothing in the catalogue for the
        warm-up to change. The Node server's ``OpcuaTools.awaitWarmUp`` is the
        same wait.
        """
        warm_up = self.warm_up
        if warm_up is None or warm_up.done():
            return
        remaining = self._warm_up_deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            # `wait`, not `wait_for`: running out of patience must not cancel it.
            await asyncio.wait({warm_up}, timeout=remaining)

    @property
    def policy(self) -> ToolPolicy:
        """The policy this server authorizes against.

        One object per server, because the connection re-binds its namespace
        mapping on every (re)connect while ``call_tool`` authorizes against it —
        see #105 for what a stale mapping costs. The Node half wires the same
        single object into both halves for the same reason.

        Resolved on first use rather than in ``__init__``: ``tool_policy()``
        parses the environment and raises on a bad configuration, and importing
        this package has to stay side-effect free (see the note in
        ``__init__.py``). ``main`` is the one place allowed to fail on that, so it
        can print the reason instead of dying inside an import.
        """
        if self._policy is None:
            self._policy = tool_policy()
        return self._policy

    @property
    def session_id(self) -> str | None:
        """The id of the session this server holds, or None while it holds none."""
        return self.connection.session_id if self.connection is not None else None

    def forget_capabilities(self) -> None:
        """Drop what was probed, because the session it was true of is gone."""
        self.capabilities = CapabilityAnswers()
        self.operation_limits = dict(UNSTATED)
        self.node_metadata.server_limits = dict(UNSTATED)
