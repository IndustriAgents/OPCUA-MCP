"""Status orchestration over a native-free diagnostics port."""

from __future__ import annotations

from typing import Protocol

from ..errors import describe_error, message


def disconnected_status(
    endpoint_url: str, security: str, server_identity: dict, error: str | None
) -> dict:
    """The report for a connection that is not up: configuration, and why.

    ``server_identity`` is reported here too: it comes from configuration, and
    "why are the control tools missing?" is as likely a question while the
    connection is down as while it is up.
    """
    return {
        "connected": False,
        "endpoint_url": endpoint_url,
        "security": security,
        "server_identity": server_identity,
        "server_state": None,
        "current_time": None,
        "start_time": None,
        "build_info": None,
        "diagnostics": None,
        "namespaces": [],
        "error": error,
    }


class DiagnosticsPort(Protocol):
    def snapshot(self) -> dict: ...
    async def read(self, security: str, identity: dict) -> dict: ...
    def capabilities(self) -> dict: ...


async def get_server_status(port: DiagnosticsPort, security: str, identity: dict) -> dict:
    snapshot = port.snapshot()
    if snapshot["connecting"]:
        status = disconnected_status(
            snapshot["endpoint"],
            security,
            identity,
            message(
                "stillConnecting",
                url=snapshot["endpoint"],
                reason=snapshot["lastError"] or "not yet known",
            ),
        )
    else:
        try:
            status = await port.read(security, identity)
        except Exception as error:
            status = disconnected_status(
                snapshot["endpoint"], security, identity, describe_error(error)
            )
    return {**status, "capabilities": port.capabilities()}
