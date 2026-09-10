"""Unit tests for the Python server's OPC UA security configuration.

No OPC UA server and no MCP transport: this is the pure environment → config
layer. It is worth testing hard because a misread here either downgrades a
production connection to plaintext or fails at connect time with an opaque
library error.

The Node equivalent lives in packages/server-node/test/unit.test.mjs; the two
files deliberately assert the same error wording for the checks both runtimes
share.
"""

from __future__ import annotations

import pytest
from opcua_mcp_server.security import (
    SecurityConfig,
    describe_security,
    is_insecure,
    parse_security_config,
)

# Pretend every configured path exists; path checking is covered separately.
ALWAYS = lambda path: True  # noqa: E731

CERTS = {"OPCUA_CLIENT_CERT": "/pki/client.pem", "OPCUA_CLIENT_KEY": "/pki/client.key"}


def parse(env: dict[str, str]) -> SecurityConfig:
    return parse_security_config(env, exists=ALWAYS)


def test_defaults_to_no_security():
    """Unchanged from the hardcoded behaviour, so existing deployments keep working."""
    config = parse({})
    assert (config.policy, config.mode) == ("None", "None")
    assert config.username is None and config.password is None
    assert is_insecure(config)


def test_blank_values_count_as_unset():
    """MCP client configs routinely carry empty env entries."""
    config = parse({"OPCUA_SECURITY_POLICY": "", "OPCUA_SECURITY_MODE": "  "})
    assert (config.policy, config.mode) == ("None", "None")


def test_a_policy_alone_implies_the_strongest_mode():
    """Silently signing when the operator asked for a policy would be a downgrade."""
    config = parse({"OPCUA_SECURITY_POLICY": "Basic256Sha256", **CERTS})
    assert (config.policy, config.mode) == ("Basic256Sha256", "SignAndEncrypt")
    assert not is_insecure(config)


def test_explicit_sign_mode_is_kept():
    config = parse({"OPCUA_SECURITY_POLICY": "Basic256", "OPCUA_SECURITY_MODE": "Sign", **CERTS})
    assert (config.policy, config.mode) == ("Basic256", "Sign")


@pytest.mark.parametrize(
    ("given", "expected"),
    [("basic256sha256", "Basic256Sha256"), ("BASIC256", "Basic256"), ("none", "None")],
)
def test_policy_names_are_case_insensitive(given, expected):
    config = parse({"OPCUA_SECURITY_POLICY": given, **CERTS})
    assert config.policy == expected


def test_mode_names_are_case_insensitive():
    config = parse(
        {
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_SECURITY_MODE": "signandencrypt",
            **CERTS,
        }
    )
    assert config.mode == "SignAndEncrypt"


def test_rejects_an_unknown_policy():
    with pytest.raises(ValueError) as excinfo:
        parse({"OPCUA_SECURITY_POLICY": "Basic999"})
    assert str(excinfo.value) == (
        'Invalid OPCUA_SECURITY_POLICY: "Basic999". '
        "Use one of: None, Basic128Rsa15, Basic256, Basic256Sha256"
    )


def test_rejects_a_node_only_policy_by_name():
    """python-opcua has no AES suites — say so instead of "invalid policy"."""
    with pytest.raises(ValueError) as excinfo:
        parse({"OPCUA_SECURITY_POLICY": "Aes256_Sha256_RsaPss", **CERTS})
    assert str(excinfo.value) == (
        'Security policy "Aes256_Sha256_RsaPss" is supported only by the Node runtime; '
        "the Python runtime (python-opcua) supports: None, Basic128Rsa15, Basic256, Basic256Sha256"
    )


def test_rejects_an_unknown_mode():
    with pytest.raises(ValueError) as excinfo:
        parse({"OPCUA_SECURITY_MODE": "Encrypt"})
    assert str(excinfo.value) == (
        'Invalid OPCUA_SECURITY_MODE: "Encrypt". Use one of: None, Sign, SignAndEncrypt'
    )


def test_rejects_a_mode_without_a_policy():
    """SecurityPolicy#None only ever pairs with MessageSecurityMode None."""
    with pytest.raises(ValueError) as excinfo:
        parse({"OPCUA_SECURITY_MODE": "SignAndEncrypt"})
    assert str(excinfo.value) == (
        "OPCUA_SECURITY_MODE=SignAndEncrypt requires OPCUA_SECURITY_POLICY to be set to a "
        "policy other than None"
    )


def test_rejects_a_policy_with_mode_none():
    with pytest.raises(ValueError) as excinfo:
        parse({"OPCUA_SECURITY_POLICY": "Basic256Sha256", "OPCUA_SECURITY_MODE": "None", **CERTS})
    assert str(excinfo.value) == (
        "OPCUA_SECURITY_POLICY=Basic256Sha256 cannot be combined with OPCUA_SECURITY_MODE=None; "
        "use Sign or SignAndEncrypt"
    )


@pytest.mark.parametrize(
    "env",
    [{}, {"OPCUA_CLIENT_CERT": "/pki/client.pem"}, {"OPCUA_CLIENT_KEY": "/pki/client.key"}],
    ids=["neither", "cert only", "key only"],
)
def test_requires_a_certificate_and_key_for_a_secure_policy(env):
    with pytest.raises(ValueError) as excinfo:
        parse({"OPCUA_SECURITY_POLICY": "Basic256Sha256", **env})
    assert str(excinfo.value) == (
        "OPCUA_SECURITY_POLICY=Basic256Sha256 requires OPCUA_CLIENT_CERT and OPCUA_CLIENT_KEY "
        "(paths to the client certificate and its private key)"
    )


def test_reports_a_missing_certificate_file(tmp_path):
    """Caught here rather than as an opaque error from the crypto layer."""
    key = tmp_path / "client.key"
    key.write_text("key")
    missing = tmp_path / "client.pem"
    with pytest.raises(ValueError) as excinfo:
        parse_security_config(
            {
                "OPCUA_SECURITY_POLICY": "Basic256Sha256",
                "OPCUA_CLIENT_CERT": str(missing),
                "OPCUA_CLIENT_KEY": str(key),
            }
        )
    assert str(excinfo.value) == f"OPCUA_CLIENT_CERT does not exist: {missing}"


def test_accepts_existing_certificate_files(tmp_path):
    cert, key = tmp_path / "client.pem", tmp_path / "client.key"
    cert.write_text("cert")
    key.write_text("key")
    config = parse_security_config(
        {
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_CLIENT_CERT": str(cert),
            "OPCUA_CLIENT_KEY": str(key),
        }
    )
    assert (config.client_cert, config.client_key) == (str(cert), str(key))


def test_application_uri_is_optional_and_passed_through():
    """Servers may reject a session whose URI does not match the certificate's."""
    assert parse({}).application_uri is None
    config = parse({"OPCUA_APPLICATION_URI": "urn:plant:mcp-client", **CERTS})
    assert config.application_uri == "urn:plant:mcp-client"


def test_credentials_are_read_together():
    config = parse({"OPCUA_USERNAME": "operator", "OPCUA_PASSWORD": "hunter2"})
    assert (config.username, config.password) == ("operator", "hunter2")
    # A user identity is authentication even without a security policy.
    assert not is_insecure(config)


def test_an_empty_password_is_a_password():
    """Unlike the other variables, "" here is an explicit (if unwise) credential."""
    config = parse({"OPCUA_USERNAME": "operator", "OPCUA_PASSWORD": ""})
    assert config.password == ""


def test_rejects_a_username_without_a_password():
    with pytest.raises(ValueError) as excinfo:
        parse({"OPCUA_USERNAME": "operator"})
    assert str(excinfo.value) == "OPCUA_USERNAME requires OPCUA_PASSWORD"


def test_rejects_a_password_without_a_username():
    with pytest.raises(ValueError) as excinfo:
        parse({"OPCUA_PASSWORD": "hunter2"})
    assert str(excinfo.value) == "OPCUA_PASSWORD requires OPCUA_USERNAME"


def test_description_never_leaks_the_password():
    config = parse(
        {
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_USERNAME": "operator",
            "OPCUA_PASSWORD": "hunter2",
            **CERTS,
        }
    )
    described = describe_security(config)
    assert described == 'policy=Basic256Sha256 mode=SignAndEncrypt user="operator"'
    assert "hunter2" not in described


def test_description_of_the_default_connection():
    assert describe_security(parse({})) == "policy=None mode=None user=anonymous"
