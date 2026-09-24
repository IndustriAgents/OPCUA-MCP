"""When control may travel over the OPC UA connection (#134).

The control gate used to check that the channel was *encrypted*, while its
safety meaning needs the server to be *authenticated*: both client libraries
encrypt happily to whatever certificate the endpoint presents, so an attacker
able to answer for the endpoint got an encrypted channel and, with it, the
control tools. Control now needs a verified server identity — a secured channel
and a pinned ``OPCUA_SERVER_CERT`` — or one of two explicit lab overrides, each
covering exactly one missing property.

The rule itself lives in ``tests/fixtures/control-gate.json``, every combination
of profile, channel, pin and override, and
``packages/server-node/test/control-gate.test.mjs`` drives the same table.
What is here beyond it is what a table cannot say: that the table is complete,
that every control tool shares the gate, that the refusals are actionable, and
that an expired pin fails closed.
"""

from __future__ import annotations

import datetime
import itertools
import json

import pytest
from conftest import ROOT
from fixtures.pki import write_self_signed
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.errors import message
from opcua_mcp_server.policy import (
    CONTROL_GATES,
    ToolPolicy,
    control_gate,
    describe_policy,
    parse_policy_config,
    server_identity_record,
)
from opcua_mcp_server.security import pinned_certificate_problem

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "control-gate.json").read_text("utf-8"))
CASES = FIXTURE["cases"]
TOOL = FIXTURE["tool"]
TOOLS = {tool["name"]: tool for tool in CONTRACT["tools"]}
CONTROL_TOOLS = [
    tool for tool in CONTRACT["tools"] if tool["accessClass"] in {"control", "alarm-action"}
]


def policy(env: dict[str, str]) -> ToolPolicy:
    subject = ToolPolicy(parse_policy_config(env))
    subject.bind_namespaces(["http://opcfoundation.org/UA/", "urn:one", "urn:plant"])
    return subject


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_control_gate_from_the_shared_table(case):
    subject = policy(case["env"])

    assert control_gate(subject.config) == case["control"]
    assert server_identity_record(subject.config) == case["server_identity"]
    assert describe_policy(subject) == case["startup"]
    assert subject.is_visible(TOOLS[TOOL]) is case["offered"]

    if case["refusal"] is None:
        subject.authorize(TOOL, FIXTURE["call"])
        return
    with pytest.raises(PermissionError) as refused:
        subject.authorize(TOOL, FIXTURE["call"])
    # The same sentence the Node half asserts, from the same contract template.
    assert str(refused.value) == message(
        case["refusal"], tool=TOOL, profile=case["env"]["OPCUA_PROFILE"]
    )


def test_the_table_covers_every_combination():
    """A table that quietly lost a row is a rule nobody is checking any more."""

    def key(env):
        return (
            env["OPCUA_PROFILE"],
            env["OPCUA_SECURITY_POLICY"],
            env.get("OPCUA_SECURITY_MODE"),
            "OPCUA_SERVER_CERT" in env,
            env.get("OPCUA_ALLOW_INSECURE_CONTROL") == "true",
            env.get("OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL") == "true",
        )

    seen = {key(case["env"]) for case in CASES}
    channels = [
        ("None", None, False),
        ("Basic256Sha256", "Sign", False),
        ("Basic256Sha256", "SignAndEncrypt", False),
        ("Basic256Sha256", "Sign", True),
        ("Basic256Sha256", "SignAndEncrypt", True),
    ]
    expected = {
        (profile, *channel, insecure, unverified)
        for profile, channel, insecure, unverified in itertools.product(
            ("observe", "operator", "full"), channels, (False, True), (False, True)
        )
    }
    assert seen == expected
    assert len(CASES) == len(expected)
    assert {case["control"] for case in CASES} == set(CONTROL_GATES)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_control_tool_shares_the_gate(case):
    """The gate is not a property of one tool: under ``full`` it opens or closes
    every write, method call and alarm action together."""
    env = {**case["env"], "OPCUA_PROFILE": "full"}
    subject = policy(env)
    open_ = control_gate(subject.config) != "blocked"
    assert {tool["name"]: subject.is_visible(tool) for tool in CONTROL_TOOLS} == {
        tool["name"]: open_ for tool in CONTROL_TOOLS
    }


@pytest.mark.parametrize(
    ("key", "remedies"),
    [
        ("controlNeedsSecureChannel", ["OPCUA_SECURITY_POLICY", "OPCUA_ALLOW_INSECURE_CONTROL"]),
        (
            "controlNeedsVerifiedServer",
            ["OPCUA_SERVER_CERT", "OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL"],
        ),
    ],
)
def test_a_refusal_names_the_variable_that_would_open_it(key, remedies):
    """A bare "disabled", with no way forward, sends an operator to the source."""
    text = message(key, tool=TOOL)
    for remedy in remedies:
        assert remedy in text


def test_the_insecure_override_does_not_vouch_for_an_unverified_server():
    """The regression the issue is about, stated on its own.

    A deployment that set ``OPCUA_ALLOW_INSECURE_CONTROL`` for a lab and then
    turned encryption on must not find that the override now also accepts a
    server nobody verified — and the refusal has to say which variable does.
    """
    subject = policy(
        {
            "OPCUA_PROFILE": "full",
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
        }
    )
    assert control_gate(subject.config) == "blocked"
    with pytest.raises(PermissionError, match="OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL"):
        subject.authorize(TOOL, FIXTURE["call"])


def test_a_tool_the_allowlist_excludes_is_not_blamed_on_the_channel():
    """Telling an operator to pin a certificate for a tool they excluded would
    be advice that changes nothing."""
    subject = policy(
        {
            "OPCUA_PROFILE": "full",
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_ALLOWED_TOOLS": "read_opcua_nodes",
        }
    )
    with pytest.raises(PermissionError) as refused:
        subject.authorize(TOOL, FIXTURE["call"])
    assert str(refused.value) == message("toolDisabled", tool=TOOL, profile="full")


def test_the_unverified_override_can_come_from_the_policy_file(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps({"version": 1, "profile": "full", "allow_unverified_server_control": True}),
        encoding="utf-8",
    )
    subject = policy({"OPCUA_POLICY_FILE": str(path), "OPCUA_SECURITY_POLICY": "Basic256Sha256"})
    assert control_gate(subject.config) == "UNVERIFIED-OVERRIDE"

    # And the environment overrides the file, as it does for every other key.
    overridden = policy(
        {
            "OPCUA_POLICY_FILE": str(path),
            "OPCUA_SECURITY_POLICY": "Basic256Sha256",
            "OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL": "false",
        }
    )
    assert control_gate(overridden.config) == "blocked"


def test_the_unverified_override_must_be_a_boolean():
    with pytest.raises(ValueError, match="OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL"):
        parse_policy_config({"OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL": "maybe"})


# --- a pin outside its validity window fails closed ------------------------------

NOW = datetime.datetime.now(datetime.timezone.utc)
DAY = datetime.timedelta(days=1)


def test_a_current_pin_has_no_problem(tmp_path):
    cert, _ = write_self_signed(tmp_path, "server", "urn:test:server")
    assert pinned_certificate_problem(str(cert)) is None


def test_an_expired_pin_is_refused_with_its_expiry(tmp_path):
    expired_at = (NOW - DAY).replace(microsecond=0)
    cert, _ = write_self_signed(
        tmp_path, "server", "urn:test:server", not_before=NOW - 30 * DAY, not_after=expired_at
    )
    problem = pinned_certificate_problem(str(cert))
    assert problem is not None
    assert f"expired on {expired_at.strftime('%Y-%m-%dT%H:%M:%SZ')}" in problem
    assert "OPCUA_SERVER_CERT" in problem


def test_a_pin_that_is_not_valid_yet_is_refused(tmp_path):
    starts = (NOW + DAY).replace(microsecond=0)
    cert, _ = write_self_signed(
        tmp_path, "server", "urn:test:server", not_before=starts, not_after=NOW + 30 * DAY
    )
    problem = pinned_certificate_problem(str(cert))
    assert problem is not None
    assert f"not valid until {starts.strftime('%Y-%m-%dT%H:%M:%SZ')}" in problem


def test_an_unreadable_pin_is_left_to_the_library(tmp_path):
    """python-opcua refuses it in its own words; guessing here would only
    reword that less precisely."""
    garbage = tmp_path / "server.pem"
    garbage.write_text("not a certificate", encoding="utf-8")
    assert pinned_certificate_problem(str(garbage)) is None
