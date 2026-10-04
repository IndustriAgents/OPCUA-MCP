"""Generate public test certificates and disposable test-only keys for #167."""

import ipaddress
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(".context/generated-trust-fixtures")
ROOT.mkdir(parents=True, exist_ok=True)
URI = "urn:opcua-mcp:trust-fixture"


def date(year):
    return datetime(year, 1, 1, tzinfo=timezone.utc)


def key(name):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if name == "server":
        (ROOT / f"{name}-test-only.key.pem").write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return private


root_key = key("root")
issuer_key = key("issuer")
leaf_key = key("server")
rogue_key = key("rogue")


def certificate(
    name,
    serial,
    private,
    issuer=None,
    signer=None,
    ca=False,
    start=2020,
    end=2040,
    uri=URI,
    host="localhost",
    path_length=None,
    critical_unknown=False,
):
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    authority = issuer.subject if issuer else subject
    signing = signer or private
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(authority)
        .public_key(private.public_key())
        .serial_number(serial)
        .not_valid_before(date(start))
        .not_valid_after(date(end))
        .add_extension(
            x509.BasicConstraints(ca=ca, path_length=path_length if ca else None), critical=True
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signing.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=not ca,
                key_encipherment=not ca,
                data_encipherment=not ca,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
    )
    if not ca:
        names = [x509.DNSName(host), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        if uri is not None:
            names.append(x509.UniformResourceIdentifier(uri))
        builder = builder.add_extension(
            x509.SubjectAlternativeName(names), critical=False
        ).add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
    if critical_unknown:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier("1.2.3.4.56789"), b"\x05\x00"),
            critical=True,
        )
    cert = builder.sign(signing, hashes.SHA256())
    (ROOT / f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert


root = certificate("root", 1, root_key, ca=True, end=2050)
issuer = certificate("issuer", 2, issuer_key, root, root_key, ca=True)
rogue = certificate("rogue", 3, rogue_key, ca=True, end=2050)
leaf = certificate("server", 10, leaf_key, issuer, issuer_key)
certificate("renewed-server", 11, leaf_key, issuer, issuer_key)
certificate("expired-server", 12, leaf_key, issuer, issuer_key, end=2025)
certificate("pending-server", 13, leaf_key, issuer, issuer_key, start=2035)
certificate("wrong-uri-server", 14, leaf_key, issuer, issuer_key, uri="urn:wrong-server")
certificate("missing-uri-server", 15, leaf_key, issuer, issuer_key, uri=None)
certificate("wrong-host-server", 16, leaf_key, issuer, issuer_key, host="other.example")
certificate("rogue-server", 17, leaf_key, rogue, rogue_key)


def crl(name, authority, signer, revoked=(), start=2025, end=2030):
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(authority.subject)
        .last_update(date(start))
        .next_update(date(end))
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False
        )
    )
    for serial in revoked:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(date(2025))
            .build()
        )
    result = builder.sign(signer, hashes.SHA256())
    (ROOT / f"{name}.crl").write_bytes(result.public_bytes(serialization.Encoding.PEM))


crl("root-current", root, root_key)
crl("issuer-current", issuer, issuer_key)
crl("issuer-revoked-leaf", issuer, issuer_key, [10])
crl("root-revoked-issuer", root, root_key, [2])
crl("issuer-expired", issuer, issuer_key, end=2026)
crl("issuer-future", issuer, issuer_key, start=2027, end=2030)
crl("issuer-forged", issuer, rogue_key)
(ROOT / "README.md").write_text(
    "Public qualification material only. Private keys are disposable test-only keys. "
    "Never use them outside tests. Certificates cover 2020-2040 except invalid cases; "
    "CRLs cover 2025-2030.\n",
    encoding="utf-8",
)
print(ROOT)

limited_root = certificate("root-limited", 30, root_key, ca=True, path_length=0, end=2050)
limited_issuer = certificate("limited-issuer", 31, issuer_key, limited_root, root_key, ca=True)
certificate("limited-server", 32, leaf_key, limited_issuer, issuer_key)
crl("root-limited-current", limited_root, root_key)
crl("limited-issuer-current", limited_issuer, issuer_key)
certificate("critical-server", 33, leaf_key, issuer, issuer_key, critical_unknown=True)
certificate("forged-server", 34, leaf_key, issuer, rogue_key)
