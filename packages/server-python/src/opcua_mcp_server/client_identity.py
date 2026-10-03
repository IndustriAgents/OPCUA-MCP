"""ApplicationDescription shared by both runtimes."""

from __future__ import annotations

CLIENT_APPLICATION_NAME = "OPC UA MCP Client"


def application_uri_problem(configured: str | None, certificate_uri: str | None) -> str | None:
    if not configured or not certificate_uri or configured == certificate_uri:
        return None
    return (
        f"OPCUA_APPLICATION_URI={configured} does not match the subjectAltName URI of "
        f"OPCUA_CLIENT_CERT ({certificate_uri}). Unset OPCUA_APPLICATION_URI or use the "
        "certificate's URI; refusing to connect with conflicting application identities."
    )
