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

from typing import Any

from .audit import AuditSink
from .config import SERVER_URL
from .connection import OpcuaConnection
from .events import EventSubscriptions
from .node_metadata import NodeMetadata
from .policy import ToolPolicy, tool_policy
from .subscriptions import SubscriptionManager


class ServerState:
    """One server's connection, capability answers, managers and audit sink.

    Constructed before the lifespan runs, because ``list_tools`` may be answered
    before any connection exists and has to say "the core tools" rather than
    fail. The lifespan fills in :attr:`connection`.
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

        #: What the connected server said it can do. Answered by the capability
        #: probes on every (re)connect, and forgotten when the session goes: a
        #: restarted server may not be the same server.
        self.capabilities: dict[str, Any] = {
            "history": False,
            "history_events": False,
            "aggregate_functions": {},
        }

        self.subscriptions = SubscriptionManager()
        self.events = EventSubscriptions()
        #: What each node published about its own number, for the life of one
        #: session. Dropped on rebind for the same reason the capabilities are.
        self.node_metadata = NodeMetadata()

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
        self.capabilities["history"] = False
        self.capabilities["history_events"] = False
        self.capabilities["aggregate_functions"] = {}

    def available_capabilities(self) -> set[str]:
        """What the connected OPC UA server reports it can do."""
        available = set()
        if self.capabilities["history"]:
            available.add("history")
        if self.capabilities["history_events"]:
            available.add("historyEvents")
        if self.capabilities["aggregate_functions"]:
            available.add("aggregate")
        return available

    def capabilities_met(self, spec: dict) -> bool:
        """Whether a tool's capability gate is satisfied.

        A tool gated on capabilities is offered when the server reports *any* of
        them. ``read_opcua_history`` lists both ``history`` and ``aggregate``: a
        server with only aggregates can still answer an aggregate read, and
        gating it on ``history`` alone would hide the one thing such a server is
        good at.
        """
        required = spec.get("capabilities") or []
        return not required or bool(set(required) & self.available_capabilities())
