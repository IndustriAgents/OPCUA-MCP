"""Both runtimes consume the same real chains, CRLs and endpoint identities."""

from __future__ import annotations

import json
import shutil
from datetime import datetime

import pytest
from conftest import ROOT
from cryptography import x509
from opcua_mcp_server.trust_store import certificate_problem

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
