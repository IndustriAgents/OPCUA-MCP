"""MCP catalogue and invocation adapters over the shared application pipeline."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..application.execution import ExecutionCall as _Call
from ..application.execution import execute_tool
from ..application.invocation import invoke_tool
from ..capabilities import refusal, requirements, verdict
from ..connection import OpcuaConnection, describe_error, is_connection_error, not_connected_message
from ..contract import CONTRACT
from ..infrastructure.control_audit import (
    _audit_after,
    _audit_permission,
    describe_targets,
    new_call_id,
)
from ..result_text import normalize_result_text
from ..state import ServerState
from .catalogue import _advertised_tool, _without_sdk_prefix


class PolicyMCPServer(MCPServer):
    """MCPServer whose advertised and callable tools obey deployment policy."""

    def __init__(
        self,
        *args,
        state: ServerState | None = None,
        signal_installer=None,
        capability_probe=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.state = state if state is not None else ServerState()
        self.signal_installer = signal_installer
        self.capability_probe = capability_probe

    async def run_stdio_async(self) -> None:
        # Install again after the SDK's event loop exists, before it starts
        # stdin and OPC UA worker threads. Preserve callers' handlers on EOF.
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.signal_installer(self.state)
        try:
            await super().run_stdio_async()
        finally:
            loop = asyncio.get_running_loop()
            for sig, handler in previous.items():
                if os.name == "posix":
                    loop.remove_signal_handler(sig)
                signal.signal(sig, handler)

    async def list_tools(self):
        """The catalogue: the contract, filtered by deployment policy only.

        No network I/O, no waiting, and the same answer for the life of the
        process whatever the plant is doing (#140). It used to depend on the
        capabilities of the session held — ``read_event_history`` absent,
        ``aggregate_function`` withheld — so a process started while the plant
        was down advertised less than one started while it was up, and nothing
        portable told a client that had listed once to list again. Clients and
        models cache tool definitions for the life of a session; what they cache
        now stays true. A capability the server lacks is reported by the call
        that needs it (:meth:`_ensure_capabilities`) and by ``get_server_status``.

        The policy is configuration, fixed when the process starts, so filtering
        on it does not make the catalogue move. No
        ``notifications/tools/list_changed`` is sent, on either runtime — see
        docs/architecture.md — and nothing depends on one.
        """
        policy = self.state.policy
        specs = {tool["name"]: tool for tool in CONTRACT["tools"]}
        listed = await super().list_tools()
        return [
            _advertised_tool(tool, specs[tool.name])
            for tool in listed
            if policy.is_visible(specs[tool.name])
        ]

    async def _ensure_capabilities(
        self, spec: dict, arguments: dict, connection: OpcuaConnection
    ) -> None:
        """Refuse a call the connected server cannot serve, before anything is sent.

        Called with a live session in hand, so it can ask rather than guess. A
        cached "yes" is trusted for the session generation it was read on: if it
        has gone stale, the server's own refusal of the request says so. Nothing
        else is taken from the cache. An answer from an older generation, an
        ``unknown``, and above all a "no" are asked again first — a "no" refuses
        without touching the network, so taken from the cache it could never find
        out that the server had come back with the feature. What is left is the
        server's own answer, and a refusal worded from the contract that says
        which capability, which session, and what to do instead (#140). The Node
        server's ``ensureCapabilities`` decides the same way.
        """
        groups = requirements(spec, arguments)
        if not groups:
            return
        answers = self.state.capabilities
        decided = verdict(groups, answers.support)
        if answers.generation != connection.session_generation or decided.outcome != "allowed":
            try:
                answers = await asyncio.to_thread(self.capability_probe, self.state, connection)
            except Exception as error:
                raise ToolError(
                    not_connected_message(connection.url, describe_error(error))
                ) from error
            decided = verdict(groups, answers.support)
        if decided.outcome != "allowed":
            raise ToolError(refusal(spec["name"], decided, answers, connection.url))

    async def call_tool(self, name, arguments, context=None):
        """Translate protocol errors around the shared execution envelope."""
        owner = self

        class Port:
            new_call_id = staticmethod(new_call_id)
            wait_for_connection = owner.state.await_connection_in_flight

            def authorize(self, name, arguments):
                owner.state.policy.authorize(name, arguments)

            normalize_failure = staticmethod(_without_sdk_prefix)
            normalize_result = staticmethod(normalize_result_text)

            def allowed(self, call):
                _audit_permission(
                    owner.state,
                    call.name,
                    call.arguments,
                    call_id=call.call_id,
                    attempt=call.attempt,
                )

            def after(self, call, decision, reason=""):
                _audit_after(
                    owner.state,
                    call.name,
                    call.arguments,
                    decision,
                    reason,
                    call_id=call.call_id,
                    attempt=call.attempt,
                )

            async def run(self, call):
                return await owner._run_tool(call, context)

        try:
            return await execute_tool(Port(), name, arguments or {})
        except (PermissionError, ValueError) as error:
            raise ToolError(str(error)) from error

    async def _run_tool(self, call: _Call, context):
        """Supply native and protocol callbacks to the shared recovery policy."""
        owner = self
        connection = self.state.connection
        dispatch = super().call_tool

        class Port:
            is_connection_error = staticmethod(is_connection_error)
            wait_for_warm_up = owner.state.await_warm_up

            def endpoint(self):
                return connection.url

            def session(self):
                return connection.session_id

            def has_connection(self):
                return connection is not None

            async def connect(self):
                await asyncio.to_thread(connection.ensure_connected)

            async def capabilities(self, call):
                await owner._ensure_capabilities(call.spec, call.arguments, connection)

            async def dispatch(self, call):
                return await dispatch(call.name, call.arguments, context)

            async def reconnect(self, session):
                await asyncio.to_thread(connection.reconnect, session)

            def log_recovery(self, resend):
                suffix = " and retrying once" if resend else ""
                print(
                    f"OPC UA call failed on a dead session; reconnecting{suffix}",
                    file=sys.stderr,
                )

            def targets(self, call):
                return describe_targets(call.spec, call.arguments)

            def authorize(self, call):
                owner.state.policy.authorize(call.name, call.arguments)

            def allowed(self, call):
                _audit_permission(
                    owner.state,
                    call.name,
                    call.arguments,
                    call_id=call.call_id,
                    attempt=call.attempt,
                )

            def denied(self, call, reason):
                _audit_after(
                    owner.state,
                    call.name,
                    call.arguments,
                    "denied",
                    reason,
                    call_id=call.call_id,
                    attempt=call.attempt,
                )

        return await invoke_tool(Port(), call)
