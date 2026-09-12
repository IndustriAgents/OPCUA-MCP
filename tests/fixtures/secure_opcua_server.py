"""A minimal *secured* mock OPC UA server, for the security end-to-end tests.

Deliberately separate from `packages/mock-server`, which must keep offering an
unsecured endpoint for the rest of the suite. This one offers **no** `None`
endpoint at all, so a client that negotiates no security cannot get in, and it
requires a username and password.

With `--check-client-uri`, it also rejects a session whose announced
ApplicationUri is not the `subjectAltName` URI of the certificate that session
presented. Real equipment makes that check; python-opcua's server does not, so
without it a client announcing `urn:freeopcua:client` while presenting a
certificate that says otherwise still gets in, and the tests cannot tell a
correctly derived ApplicationUri from a library default. A session that presents
no certificate at all is left to the endpoint matching to refuse, as before.

Run:
    python secure_opcua_server.py --endpoint opc.tcp://127.0.0.1:4843/mcp/secure \
        --cert server.pem --key server_key.pem --uri urn:opcua-mcp:test-server

Prints `READY <node id of the writable Double>` on stdout once it is listening.
"""

from __future__ import annotations

import argparse
import time

from cryptography import x509
from opcua import Server, ua
from opcua.common.utils import ServiceError
from opcua.server.internal_server import InternalSession
from opcua.server.user_manager import UserManager

USERNAME = "operator"
PASSWORD = "hunter2"


def certificate_uri(der: bytes) -> str | None:
    """The subjectAltName URI of a DER certificate, or None if it carries none."""
    try:
        alt_names = x509.load_der_x509_certificate(der).extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        uris = alt_names.value.get_values_for_type(x509.UniformResourceIdentifier)
    except x509.ExtensionNotFound:
        return None
    return uris[0] if uris else None


class UriCheckingSession(InternalSession):
    """Refuses a session announcing an ApplicationUri its certificate does not carry.

    python-opcua routes every CreateSession through `InternalServer.session_cls`,
    so swapping the class in is enough; raising `ServiceError` is how the
    library's own service handlers reject a request, and it reaches the client
    as a ServiceFault carrying the status code.
    """

    def create_session(self, params, sockname=None):
        presented = params.ClientCertificate
        announced = params.ClientDescription.ApplicationUri
        if presented and certificate_uri(presented) != announced:
            raise ServiceError(ua.StatusCodes.BadCertificateUriInvalid)
        return super().create_session(params, sockname=sockname)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--uri", required=True)
    parser.add_argument(
        "--check-client-uri",
        action="store_true",
        help="Reject a session whose ApplicationUri is not its certificate's subjectAltName URI.",
    )
    args = parser.parse_args()

    server = Server()
    if args.check_client_uri:
        server.iserver.session_cls = UriCheckingSession
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
