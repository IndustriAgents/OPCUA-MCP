"""CA-chain and signed offline-CRL validation before maintained session activation."""

from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from OpenSSL import crypto

MAX_FILES = 100
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * MAX_FILE_BYTES
CRITICAL_EXTENSIONS = {"2.5.29.19", "2.5.29.15", "2.5.29.17", "2.5.29.30", "2.5.29.37"}
REMEDIATION = (
    "Check the configured CA chain, current signed CRLs, endpoint hostname and "
    "OPCUA_SERVER_APPLICATION_URI; update the trust store before reconnecting."
)


def refusal(status: str) -> str:
    return f"OPCUA_SERVER_TRUST_STORE: {status}. {REMEDIATION}"


def _material(root: Path):
    material = {}
    count = total = 0
    for folder in ("trusted/certs", "issuers/certs", "trusted/crl", "issuers/crl"):
        data = []
        directory = root / folder
        if directory.exists():
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                count += 1
                if count > MAX_FILES:
                    raise ValueError("invalid bounded trust material")
                with path.open("rb") as handle:
                    raw = handle.read(MAX_FILE_BYTES + 1)
                total += len(raw)
                if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ValueError("invalid bounded trust material")
                data.append(raw)
        material[folder] = data
    return material


def _certificate(data: bytes):
    return (
        x509.load_pem_x509_certificate(data)
        if data.startswith(b"-----BEGIN")
        else x509.load_der_x509_certificate(data)
    )


def _crl(data: bytes):
    return (
        x509.load_pem_x509_crl(data)
        if data.startswith(b"-----BEGIN")
        else x509.load_der_x509_crl(data)
    )


def _openssl_status(error):
    code, depth, _ = error.errors
    if code == 23:
        return "BadCertificateIssuerRevoked" if depth else "BadCertificateRevoked"
    if code in (9, 10):
        return "BadCertificateIssuerTimeInvalid" if depth else "BadCertificateTimeInvalid"
    if code in (3, 11, 12):
        return "BadCertificateRevocationUnknown"
    if code in (18, 19, 20, 21):
        return "BadCertificateUntrusted"
    return "BadCertificateInvalid"


def _identity_problem(certificate, application_uri: str, endpoint: str, advertised_uri: str):
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return "BadCertificateUriInvalid"
    if advertised_uri != application_uri or application_uri not in names.get_values_for_type(
        x509.UniformResourceIdentifier
    ):
        return "BadCertificateUriInvalid"
    host = urlsplit(endpoint).hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() not in {name.lower() for name in names.get_values_for_type(x509.DNSName)}:
            return "BadCertificateHostNameInvalid"
    else:
        if address not in names.get_values_for_type(x509.IPAddress):
            return "BadCertificateHostNameInvalid"
    return None


def certificate_problem(
    path: str,
    certificate: x509.Certificate,
    application_uri: str,
    endpoint: str,
    advertised_uri: str,
    now: datetime | None = None,
) -> str | None:
    """Reload administrator material for each validation; never download missing issuers/CRLs."""
    moment = now or datetime.now(timezone.utc)
    try:
        material = _material(Path(path))
        anchors = [_certificate(data) for data in material["trusted/certs"]]
        issuers = [_certificate(data) for data in material["issuers/certs"]]
        if not anchors:
            return "BadCertificateUntrusted"
        for anchor in anchors:
            anchor.verify_directly_issued_by(anchor)
        try:
            usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound:
            pass
        else:
            if (
                x509.ExtendedKeyUsageOID.SERVER_AUTH not in usage
                and x509.ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE not in usage
            ):
                return "BadCertificateInvalid"
        if any(
            not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            for cert in anchors + issuers
        ):
            return "BadCertificateInvalid"
        if any(
            extension.critical and extension.oid.dotted_string not in CRITICAL_EXTENSIONS
            for cert in [certificate, *anchors, *issuers]
            for extension in cert.extensions
        ):
            return "BadCertificateInvalid"
        crls = [
            _crl(data) for folder in ("trusted/crl", "issuers/crl") for data in material[folder]
        ]
        if not crls or any(
            crl.next_update_utc is None or not crl.last_update_utc <= moment < crl.next_update_utc
            for crl in crls
        ):
            return "BadCertificateRevocationUnknown"
        if any(extension.critical for crl in crls for extension in crl.extensions):
            return "BadCertificateInvalid"
        if any(
            not any(
                crl.issuer == issuer.subject
                and issuer.extensions.get_extension_for_class(x509.KeyUsage).value.crl_sign
                and crl.is_signature_valid(issuer.public_key())
                for issuer in anchors + issuers
            )
            for crl in crls
        ):
            return "BadCertificateInvalid"
        # OpenSSL's chain engine verifies CRL signatures and issuer permissions,
        # not merely matching serial numbers in an unsigned list.
        store = crypto.X509Store()
        store.set_flags(
            crypto.X509StoreFlags.CRL_CHECK
            | crypto.X509StoreFlags.CRL_CHECK_ALL
            | crypto.X509StoreFlags.X509_STRICT
        )
        store.set_time(moment)
        for anchor in anchors:
            store.add_cert(crypto.X509.from_cryptography(anchor))
        for crl in crls:
            store.add_crl(crl)
        context = crypto.X509StoreContext(
            store,
            crypto.X509.from_cryptography(certificate),
            [crypto.X509.from_cryptography(issuer) for issuer in issuers],
        )
        context.verify_certificate()
        if len(context.get_verified_chain()) > 8:
            return "BadCertificateChainIncomplete"
        return _identity_problem(certificate, application_uri, endpoint, advertised_uri)
    except crypto.X509StoreContextError as error:
        return _openssl_status(error)
    except Exception:
        return "BadCertificateInvalid"


def validator(path: str, application_uri: str, endpoint: str):
    async def validate(certificate, description):
        # SDK callback runs before ActivateSession sends a user identity token.
        if problem := certificate_problem(
            path, certificate, application_uri, endpoint, description.ApplicationUri
        ):
            raise ValueError(refusal(problem))

    return validate
