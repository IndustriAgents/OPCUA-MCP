"""Both runtimes consume the same real chains, CRLs and endpoint identities."""

from __future__ import annotations

import json
import shutil
from datetime import datetime

import pytest
from conftest import ROOT
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from opcua_mcp_server.security import parse_security_config
from opcua_mcp_server.trust_store import certificate_problem
from OpenSSL import crypto

FIXTURES = ROOT / "tests/fixtures/trust-store"
TABLE = json.loads((ROOT / "tests/fixtures/trust-store-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", TABLE["cases"], ids=lambda case: case["id"])
def test_trust_chain_and_signed_crl_cases(tmp_path, case):
    for group, folder in (("anchors", "trusted/certs"), ("issuers", "issuers/certs")):
        directory = tmp_path / folder
        directory.mkdir(parents=True)
        for name in case[group]:
            shutil.copyfile(FIXTURES / name, directory / name)
    for name in case["crls"]:
        directory = tmp_path / ("trusted/crl" if name.startswith("root-") else "issuers/crl")
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / name, directory / name)
    certificate = x509.load_pem_x509_certificate((FIXTURES / case["certificate"]).read_bytes())
    assert (
        certificate_problem(
            str(tmp_path),
            certificate,
            case["applicationUri"],
            case["endpoint"],
            case["advertisedUri"],
            datetime.fromisoformat(TABLE["now"].replace("Z", "+00:00")),
        )
        == case["expected"]
    )


@pytest.mark.parametrize("layout", ["oversized-file", "too-many-files"])
def test_trust_material_is_bounded_before_parsing(tmp_path, layout):
    directory = tmp_path / "trusted/certs"
    directory.mkdir(parents=True)
    if layout == "oversized-file":
        (directory / "huge.pem").write_bytes(b"x" * (1024 * 1024 + 1))
    else:
        for index in range(101):
            shutil.copyfile(FIXTURES / "root.pem", directory / f"{index}.pem")
    certificate = x509.load_pem_x509_certificate((FIXTURES / "server.pem").read_bytes())
    row = TABLE["cases"][0]
    assert (
        certificate_problem(
            str(tmp_path), certificate, row["applicationUri"], row["endpoint"], row["advertisedUri"]
        )
        == "BadCertificateInvalid"
    )


def test_crl_reload_refuses_a_previously_valid_peer(tmp_path):
    row = TABLE["cases"][0]
    for folder, names in (
        ("trusted/certs", ["root.pem"]),
        ("issuers/certs", ["issuer.pem"]),
        ("trusted/crl", ["root-current.crl"]),
        ("issuers/crl", ["issuer-current.crl"]),
    ):
        directory = tmp_path / folder
        directory.mkdir(parents=True)
        for name in names:
            shutil.copyfile(FIXTURES / name, directory / name)
    certificate = x509.load_pem_x509_certificate((FIXTURES / "server.pem").read_bytes())

    def check():
        return certificate_problem(
            str(tmp_path),
            certificate,
            row["applicationUri"],
            row["endpoint"],
            row["advertisedUri"],
            datetime.fromisoformat(TABLE["now"].replace("Z", "+00:00")),
        )

    assert check() is None
    shutil.copyfile(
        FIXTURES / "issuer-revoked-leaf.crl", tmp_path / "issuers/crl/issuer-current.crl"
    )
    assert check() == "BadCertificateRevoked"


@pytest.mark.parametrize("case", TABLE["configuration"], ids=lambda case: case["id"])
def test_invalid_trust_configuration_refuses_without_connecting(case):
    with pytest.raises(ValueError) as error:
        parse_security_config(case["env"], exists=lambda _: True)
    assert str(error.value) == case["error"]


def test_oversized_peer_refuses_before_crypto_path_validation(tmp_path, monkeypatch):
    row = TABLE["cases"][0]
    for folder, names in (
        ("trusted/certs", ["root.pem"]),
        ("issuers/certs", ["issuer.pem"]),
        ("trusted/crl", ["root-current.crl"]),
        ("issuers/crl", ["issuer-current.crl"]),
    ):
        directory = tmp_path / folder
        directory.mkdir(parents=True)
        for name in names:
            shutil.copyfile(FIXTURES / name, directory / name)
    source = x509.load_pem_x509_certificate((FIXTURES / row["certificate"]).read_bytes())
    builder = (
        x509.CertificateBuilder()
        .subject_name(source.subject)
        .issuer_name(source.issuer)
        .public_key(source.public_key())
        .serial_number(source.serial_number)
        .not_valid_before(source.not_valid_before_utc)
        .not_valid_after(source.not_valid_after_utc)
    )
    for extension in source.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    payload = bytes(TABLE["peerCertificateBytes"])
    certificate = builder.add_extension(
        x509.UnrecognizedExtension(
            x509.ObjectIdentifier("1.2.3.4.56790"),
            b"\x04\x83" + len(payload).to_bytes(3, "big") + payload,
        ),
        critical=False,
    ).sign(
        serialization.load_pem_private_key(
            (FIXTURES / "server-test-only.key.pem").read_bytes(), password=None
        ),
        hashes.SHA256(),
    )
    assert len(certificate.public_bytes(serialization.Encoding.DER)) > TABLE["peerCertificateBytes"]
    checked = []

    def verify(context):
        checked.append(True)
        raise AssertionError("Over-budget peer must not reach path validation")

    monkeypatch.setattr(crypto.X509StoreContext, "verify_certificate", verify)
    assert (
        certificate_problem(
            str(tmp_path),
            certificate,
            row["applicationUri"],
            row["endpoint"],
            row["advertisedUri"],
            datetime.fromisoformat(TABLE["now"].replace("Z", "+00:00")),
        )
        == "BadCertificateInvalid"
    )
    assert not checked, "Over-budget peer reached cryptographic path validation"
