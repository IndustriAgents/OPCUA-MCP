"""Unit tests for the Python server's OPC UA security configuration.

No OPC UA server and no MCP transport: this is the environment (and, at the end,
the client certificate) → config layer. It is worth testing hard because a
misread here either downgrades a production connection to plaintext or fails at
connect time with an opaque library error.

The Node equivalent lives in packages/server-node/test/unit.test.mjs; the two
files deliberately assert the same error wording for the checks both runtimes
share.
"""

from __future__ import annotations

import pytest
from fixtures.pki import write_self_signed
from opcua import Client
from opcua_mcp_server import security
from opcua_mcp_server.security import (
    SecurityConfig,
    certificate_application_uri,
    describe_security,
    parse_security_config,
    security_warnings,
)

# Pretend every configured path exists; path checking is covered separately.
ALWAYS = lambda path: True  # noqa: E731

CERTS = {"OPCUA_CLIENT_CERT": "/pki/client.pem", "OPCUA_CLIENT_KEY": "/pki/client.key"}

#: A *fully* secured configuration: encrypted channel **and** a pinned server
#: certificate. `CERTS` alone is no longer that — an unpinned server certificate
#: is encryption to whoever answered, and now says so (#45).
PINNED = {**CERTS, "OPCUA_SERVER_CERT": "/pki/server.pem"}


def parse(env: dict[str, str]) -> SecurityConfig:
    return parse_security_config(env, exists=ALWAYS)


def test_defaults_to_no_security():
    """Unchanged from the hardcoded behaviour, so existing deployments keep working."""
    config = parse({})
    assert (config.policy, config.mode) == ("None", "None")
    assert config.username is None and config.password is None
    assert len(security_warnings(config)) == 1


def test_blank_values_count_as_unset():
    """MCP client configs routinely carry empty env entries."""
    config = parse({"OPCUA_SECURITY_POLICY": "", "OPCUA_SECURITY_MODE": "  "})
    assert (config.policy, config.mode) == ("None", "None")


def test_a_policy_alone_implies_the_strongest_mode():
    """Silently signing when the operator asked for a policy would be a downgrade."""
    config = parse({"OPCUA_SECURITY_POLICY": "Basic256Sha256", **CERTS})
    assert (config.policy, config.mode) == ("Basic256Sha256", "SignAndEncrypt")
    assert not any("unencrypted" in warning for warning in security_warnings(config))


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


def test_blank_credentials_mean_anonymous_rather_than_a_usage_error():
    """A blank password with no username can only mean "not configured".

    An empty password *is* a credential when paired with a username (above), but
    an empty one on its own is how "unset" arrives from an MCP client config —
    those routinely carry empty ``env`` entries — and from an MCP bundle, where
    every optional ``user_config`` field substitutes as an empty string. Treating
    that as a usage error made an anonymous connection impossible to express in
    the bundle: it refused to start until the user typed a username *and* a
    password.
    """
    assert parse({"OPCUA_USERNAME": "", "OPCUA_PASSWORD": ""}).username is None
    assert parse({"OPCUA_USERNAME": "", "OPCUA_PASSWORD": ""}).password is None
    assert parse({"OPCUA_PASSWORD": ""}).password is None


def test_warns_whenever_the_channel_is_unencrypted():
    """A username authenticates the session; it does not encrypt anything."""
    warnings = security_warnings(parse({"OPCUA_USERNAME": "operator", "OPCUA_PASSWORD": "x"}))
    assert any("traffic is unencrypted" in warning for warning in warnings)


def test_warns_that_credentials_cross_an_unencrypted_channel():
    """Both client libraries send the password in clear text on a `None` channel."""
    warnings = security_warnings(parse({"OPCUA_USERNAME": "operator", "OPCUA_PASSWORD": "x"}))
    assert any("clear text" in warning for warning in warnings)
    # ...and that extra warning is specific to having credentials configured.
    assert not any("clear text" in warning for warning in security_warnings(parse({})))


def test_an_encrypted_channel_still_warns_while_the_server_is_unverified():
    """Encryption without pinning is encryption to whoever answered (#45).

    This used to assert silence, which was the bug: `policy=Basic256Sha256`
    reads like the connection is safe, and the one thing it does not establish
    is *who* is on the other end.
    """
    config = parse(
        {
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_USERNAME": "operator",
            "OPCUA_PASSWORD": "hunter2",
            **CERTS,
        }
    )
    [warning] = security_warnings(config)
    assert "certificate is not being verified" in warning
    assert "impersonate the endpoint" in warning


def test_a_secured_and_pinned_channel_warns_about_nothing():
    config = parse(
        {
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_USERNAME": "operator",
            "OPCUA_PASSWORD": "hunter2",
            **PINNED,
        }
    )
    assert security_warnings(config) == []


def test_refuses_to_pin_a_server_certificate_on_an_unsecured_channel():
    """Ignoring it would be worse (#45).

    OPCUA_SERVER_CERT in a config file reads like protection, and with
    policy=None the server presents no certificate at all — so it would verify
    precisely nothing while looking like it verified everything.
    """
    with pytest.raises(ValueError, match="OPCUA_SERVER_CERT requires OPCUA_SECURITY_POLICY"):
        parse({"OPCUA_SERVER_CERT": "/pki/server.pem"})


def test_requires_the_user_certificate_and_its_key_together():
    base = {"OPCUA_SECURITY_POLICY": "Basic256Sha256", **CERTS}
    with pytest.raises(ValueError, match="must be set together"):
        parse({**base, "OPCUA_USER_CERT": "/pki/user.pem"})
    with pytest.raises(ValueError, match="must be set together"):
        parse({**base, "OPCUA_USER_KEY": "/pki/user.key"})


def test_refuses_certificate_and_username_identities_at_once():
    """A session carries one user identity token (#7).

    Accepting both would pick one silently, and the one not picked is the one
    the operator believes is in force.
    """
    with pytest.raises(ValueError, match="cannot be combined with OPCUA_USERNAME"):
        parse(
            {
                "OPCUA_SECURITY_POLICY": "Basic256Sha256",
                **CERTS,
                "OPCUA_USER_CERT": "/pki/user.pem",
                "OPCUA_USER_KEY": "/pki/user.key",
                "OPCUA_USERNAME": "operator",
                "OPCUA_PASSWORD": "hunter2",
            }
        )


def test_refuses_x509_user_authentication_on_an_unsecured_channel():
    with pytest.raises(ValueError, match="OPCUA_USER_CERT requires OPCUA_SECURITY_POLICY"):
        parse({"OPCUA_USER_CERT": "/pki/user.pem", "OPCUA_USER_KEY": "/pki/user.key"})


@pytest.mark.parametrize("missing", ["OPCUA_SERVER_CERT", "OPCUA_USER_CERT"])
def test_checks_that_the_new_certificate_paths_exist(missing):
    """Same treatment the client certificate already got: a typo is a startup error."""
    extra = (
        {"OPCUA_USER_CERT": "/nope.pem", "OPCUA_USER_KEY": "/nope.key"}
        if missing == "OPCUA_USER_CERT"
        else {"OPCUA_SERVER_CERT": "/nope.pem"}
    )
    with pytest.raises(ValueError, match=f"{missing} does not exist"):
        parse_security_config(
            {"OPCUA_SECURITY_POLICY": "Basic256Sha256", **CERTS, **extra},
            exists=lambda path: not path.startswith("/nope"),
        )


def test_the_startup_summary_names_the_identity_kind_and_whether_pinning_is_on():
    described = describe_security(
        parse(
            {
                "OPCUA_SECURITY_POLICY": "Basic256Sha256",
                **PINNED,
                "OPCUA_USER_CERT": "/pki/user.pem",
                "OPCUA_USER_KEY": "/pki/user.key",
            }
        )
    )
    assert "user=certificate server-cert=pinned" in described
    # ...and says nothing about pinning when it is off, so the word keeps
    # meaning something when it does appear.
    unpinned = describe_security(parse({"OPCUA_SECURITY_POLICY": "Basic256Sha256", **CERTS}))
    assert "server-cert" not in unpinned


def test_warnings_never_leak_the_password():
    config = parse({"OPCUA_USERNAME": "operator", "OPCUA_PASSWORD": "hunter2"})
    assert "hunter2" not in " ".join(security_warnings(config))


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


# --- the ApplicationUri a client announces -------------------------------------
# These read real certificate files (never a server or a socket): the URI is a
# property of the certificate, and it is what a server checks the session's
# ApplicationDescription against.


def test_reads_the_application_uri_out_of_a_certificate(tmp_path):
    cert, _ = write_self_signed(tmp_path, "client", "urn:plant:mcp-client")
    assert certificate_application_uri(str(cert)) == "urn:plant:mcp-client"


@pytest.mark.parametrize("name", ["missing.pem", "client_key.pem"])
def test_an_unreadable_certificate_yields_no_uri(tmp_path, name):
    """A missing file and a file that is not a certificate — python-opcua reports both."""
    write_self_signed(tmp_path, "client", "urn:plant:mcp-client")
    assert certificate_application_uri(str(tmp_path / name)) is None


def make_client(env: dict[str, str]) -> Client:
    """Build a client from `env`, with the process-wide config cache reset.

    ``OPCUA_SECURITY_POLICY`` stays unset on purpose: python-opcua's
    ``set_security`` connects to the server to fetch its certificate, and there
    is no server here. The ApplicationUri is chosen before any of that.
    """
    security._warned.clear()
    security.security_config.cache_clear()
    with pytest.MonkeyPatch.context() as patch:
        for name, value in env.items():
            patch.setenv(name, value)
        try:
            return security.create_client("opc.tcp://127.0.0.1:4840/none")
        finally:
            security.security_config.cache_clear()


def test_the_application_uri_defaults_to_the_certificates_own(tmp_path):
    """Without this, python-opcua announces `urn:freeopcua:client` and is refused.

    node-opcua reads the URI out of the certificate, so the same files and the
    same variables have to reach the server as the same identity here.
    """
    cert, key = write_self_signed(tmp_path, "client", "urn:plant:mcp-client")
    client = make_client({"OPCUA_CLIENT_CERT": str(cert), "OPCUA_CLIENT_KEY": str(key)})
    assert client.application_uri == "urn:plant:mcp-client"


def test_an_explicit_application_uri_wins(tmp_path):
    """Configuration is never silently ignored — certificates without a URI need it."""
    cert, key = write_self_signed(tmp_path, "client", "urn:plant:mcp-client")
    client = make_client(
        {
            "OPCUA_CLIENT_CERT": str(cert),
            "OPCUA_CLIENT_KEY": str(key),
            "OPCUA_APPLICATION_URI": "urn:plant:something-else",
        }
    )
    assert client.application_uri == "urn:plant:something-else"


def test_an_explicit_application_uri_that_contradicts_the_certificate_warns(tmp_path, capsys):
    """It is announced as asked, but a server checking the two will refuse the session."""
    cert, key = write_self_signed(tmp_path, "client", "urn:plant:mcp-client")
    make_client(
        {
            "OPCUA_CLIENT_CERT": str(cert),
            "OPCUA_CLIENT_KEY": str(key),
            "OPCUA_APPLICATION_URI": "urn:plant:something-else",
        }
    )
    printed = capsys.readouterr()
    assert "BadCertificateUriInvalid" in printed.err
    assert "urn:plant:mcp-client" in printed.err
    # stdout is the MCP stdio transport; anything printed there corrupts it.
    assert printed.out == ""


def test_a_matching_application_uri_says_nothing(tmp_path, capsys):
    cert, key = write_self_signed(tmp_path, "client", "urn:plant:mcp-client")
    make_client(
        {
            "OPCUA_CLIENT_CERT": str(cert),
            "OPCUA_CLIENT_KEY": str(key),
            "OPCUA_APPLICATION_URI": "urn:plant:mcp-client",
        }
    )
    assert capsys.readouterr().err == ""
