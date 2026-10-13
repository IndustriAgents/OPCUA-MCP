"""Common authorization/audit envelope; transport and native recovery are injected."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..contract import CONTRACT
from ..errors import ApplicationRefusal, describe_error, message
from ..limits import check_request_bounds
from ..validation import validate_arguments


@dataclass
class ExecutionCall:
    name: str
    arguments: dict[str, Any]
    spec: dict[str, Any]
    call_id: str
    attempt: int = 1
    session: str | None = None
    denied: bool = False


class ExecutionPort(Protocol):
    def new_call_id(self) -> str: ...
    async def wait_for_connection(self) -> None: ...
    def authorize(self, name: str, arguments: dict) -> None: ...
    def allowed(self, call: ExecutionCall) -> None: ...
    def after(self, call: ExecutionCall, decision: str, reason: str = "") -> None: ...
    async def run(self, call: ExecutionCall) -> Any: ...
    def normalize_failure(self, name: str, error: Exception) -> Exception: ...
    def normalize_result(self, result: Any) -> Any: ...


async def execute_tool(port: ExecutionPort, name: str, arguments: dict) -> Any:
    call_id = port.new_call_id()
    spec = next((tool for tool in CONTRACT["tools"] if tool["name"] == name), None)
    call = ExecutionCall(name, arguments, spec or {}, call_id)
    try:
        if spec is None:
            raise ApplicationRefusal(message("unknownTool", tool=name))
        check_request_bounds(name, arguments)
        validate_arguments(name, spec["inputSchema"], arguments)
        if name != "get_server_status":
            await port.wait_for_connection()
        port.authorize(name, arguments)
    except (PermissionError, ValueError) as error:
        call.denied = True
        port.after(call, "denied", str(error))
        raise
    port.allowed(call)
    try:
        result = await port.run(call)
    except Exception as error:
        reported = port.normalize_failure(name, error)
        if not call.denied:
            port.after(call, "failed", describe_error(reported))
        raise reported from error.__cause__
    port.after(call, "completed")
    return port.normalize_result(result)
