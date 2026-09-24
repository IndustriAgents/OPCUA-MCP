"""Self-signed OPC UA certificates for the secure end-to-end tests.

Generated per test session into a temporary directory rather than committed:
a checked-in key pair is a key pair someone will eventually reuse, and these
expire.

Note the `.pem` extension on *both* files. python-opcua picks PEM vs DER by
extension alone, so a PEM key named `client.key` fails to load — the same trap
the Configuration section of the README warns about.

Runnable, to try a secured connection by hand (see docs/testing.md):

    python tests/fixtures/pki.py /tmp/opcua-pki
"""

from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def write_self_signed(
    directory: Path,
    name: str,
    application_uri: str,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
) -> tuple[Path, Path]:
    """Write ``<name>.pem`` / ``<name>_key.pem`` and return both paths.

    ``application_uri`` goes into the subjectAltName, where OPC UA requires it:
    a peer may reject a session whose ApplicationDescription URI does not match
    the certificate it presented.

    The validity window defaults to a day either side of now; the bounds are
    settable so a test can pin an expired or not-yet-valid certificate (#134).
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OPCUA-MCP tests"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - datetime.timedelta(days=1))
        .not_valid_after(not_after or now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            # dataEncipherment and keyEncipherment are what the OPC UA security
            # policies actually use; keyCertSign keeps python-opcua's own checks
            # happy for a self-signed certificate.
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=True,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(application_uri),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = directory / f"{name}.pem"
    key_path = directory / f"{name}_key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


#: Application URIs the secured mock and its clients announce. They must match
#: the subjectAltName of the certificate each side presents.
SERVER_URI = "urn:opcua-mcp:test-server"
CLIENT_URI = "urn:opcua-mcp:test-client"


def main() -> None:
    """Write a server and a client key pair into the directory given on argv."""
    import sys

    directory = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    directory.mkdir(parents=True, exist_ok=True)
    for name, uri in (("server", SERVER_URI), ("client", CLIENT_URI)):
        cert, key = write_self_signed(directory, name, uri)
        print(f"{name}: {cert} {key} ({uri})")


if __name__ == "__main__":
    main()
