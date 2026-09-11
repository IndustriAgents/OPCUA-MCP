"""A minimal *secured* mock OPC UA server, for the security end-to-end tests.

Deliberately separate from `packages/mock-server`, which must keep offering an
unsecured endpoint for the rest of the suite. This one offers **no** `None`
endpoint at all, so a client that negotiates no security cannot get in, and it
requires a username and password.

Run:
    python secure_opcua_server.py --endpoint opc.tcp://127.0.0.1:4843/mcp/secure \
        --cert server.pem --key server_key.pem --uri urn:opcua-mcp:test-server

Prints `READY <node id of the writable Double>` on stdout once it is listening.
"""

from __future__ import annotations

import argparse
import time

from opcua import Server, ua
from opcua.server.user_manager import UserManager

USERNAME = "operator"
PASSWORD = "hunter2"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--uri", required=True)
    args = parser.parse_args()

    server = Server()
    server.set_endpoint(args.endpoint)
    server.set_server_name("OPCUA-MCP secure mock")
    server.set_application_uri(args.uri)
    server.load_certificate(args.cert)
    server.load_private_key(args.key)
    # No NoSecurity entry: an unsecured client has no endpoint to fall back to.
    server.set_security_policy(
        [
            ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
            ua.SecurityPolicyType.Basic256Sha256_Sign,
        ]
    )
    server.set_security_IDs(["Username"])

    def authenticate(isession, username: str, password: str) -> bool:
        isession.user = UserManager.User
        return username == USERNAME and password == PASSWORD

    server.user_manager.set_user_manager(authenticate)

    idx = server.register_namespace("http://opcua-mcp.test/secure")
    plant = server.nodes.objects.add_object(idx, "Plant")
    temperature = plant.add_variable(idx, "Temperature", 21.5)
    temperature.set_writable()

    server.start()
    print(f"READY {temperature.nodeid.to_string()}", flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
