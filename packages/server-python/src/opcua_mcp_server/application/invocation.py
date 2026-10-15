"""One connected attempt and contract-directed recovery through injected services."""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import ApplicationRefusal, UncertainOutcome, describe_error, message
from .execution import ExecutionCall


class InvocationPort(Protocol):
    def endpoint(self) -> str: ...
    def session(self) -> str | None: ...
    def has_connection(self) -> bool: ...
    async def wait_for_warm_up(self) -> None: ...
    async def connect(self) -> None: ...
    async def capabilities(self, call: ExecutionCall) -> None: ...
    async def dispatch(self, call: ExecutionCall) -> Any: ...
    def is_connection_error(self, error: Exception) -> bool: ...
    async def reconnect(self, session: str | None) -> None: ...
    def log_recovery(self, resend: bool) -> None: ...
    def targets(self, call: ExecutionCall) -> str: ...
    def authorize(self, call: ExecutionCall) -> None: ...
    def allowed(self, call: ExecutionCall) -> None: ...
    def denied(self, call: ExecutionCall, reason: str) -> None: ...


async def invoke_tool(port: InvocationPort, call: ExecutionCall) -> Any:
    if call.name == "get_server_status" or not port.has_connection():
        await port.wait_for_warm_up()
        if call.name != "get_server_status":
            _permit(port, call)
        return await port.dispatch(call)
    try:
        await port.connect()
    except Exception as error:
        raise ApplicationRefusal(
            message("notConnected", url=port.endpoint(), reason=describe_error(error))
        ) from error
    await port.capabilities(call)
    call.session = port.session()
    _permit(port, call)
    try:
        return await port.dispatch(call)
    except Exception as error:
        if isinstance(error, UncertainOutcome):
            raise
        if not port.is_connection_error(error):
            raise
        return await _recover(port, call, error)


async def _recover(port: InvocationPort, call: ExecutionCall, error: Exception) -> Any:
    retry = call.spec["retryPolicy"]
    port.log_recovery(retry == "resend")
    try:
        await port.reconnect(call.session)
    except Exception as rebuild_failed:
        if retry == "uncertainOutcome":
            raise UncertainOutcome(
                message(
                    "uncertainOutcome",
                    tool=call.name,
                    reason=(
                        f"{describe_error(error)}; recovery failed: "
                        f"{describe_error(rebuild_failed)}"
                    ),
                    targets=port.targets(call),
                )
            ) from error
        raise ApplicationRefusal(
            message("notConnected", url=port.endpoint(), reason=describe_error(rebuild_failed))
        ) from rebuild_failed
    if retry == "uncertainOutcome":
        raise UncertainOutcome(
            message(
                "uncertainOutcome",
                tool=call.name,
                reason=describe_error(error),
                targets=port.targets(call),
            )
        ) from error
    if retry != "resend":
        raise error
    call.attempt = 2
    await port.capabilities(call)
    call.session = port.session()
    _permit(port, call)
    return await port.dispatch(call)


def _permit(port: InvocationPort, call: ExecutionCall) -> None:
    """Authorize and durably record the session selected after all preparation."""
    try:
        port.authorize(call)
    except (PermissionError, ValueError) as denial:
        call.denied = True
        port.denied(call, str(denial))
        raise
    try:
        port.allowed(call)
    except Exception:
        call.denied = True
        raise
