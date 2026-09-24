"""What a malformed ``OPCUA_POLICY_FILE`` is told, from the table both runtimes share (#157).

``packages/server-node/test/policy-file-validation.test.mjs`` reads the same
``tests/fixtures/policy-file-validation.json`` and asserts the same verdict and
the same sentence. Before it existed each runtime did whatever its own code
happened to do with a surprise: a ``KeyError`` traceback here, a silent
``undefined|undefined`` allowlist entry there, and on both a flag of ``"false"``
that switched insecure control *on*.
"""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.policy import PolicyConfig, parse_policy_config

CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "policy-file-validation.json").read_text(encoding="utf-8")
)["cases"]


def _settings(config: PolicyConfig) -> dict:
    """The settings ``expect`` can name, spelled as the table spells them."""
    return {
        "profile": config.profile,
        "allowed_tools": None if config.allowed_tools is None else sorted(config.allowed_tools),
        "writable_nodes": sorted(config.writable_nodes),
        "callable_methods": sorted(config.callable_methods),
        "acknowledge_alarms": config.acknowledge_alarms,
        "allow_insecure_control": config.allow_insecure_control,
        "allow_out_of_range_writes": config.allow_out_of_range_writes,
    }


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_policy_file_parser_answers_the_shared_table(case, tmp_path):
    path = tmp_path / "policy.json"
    text = case["text"] if "text" in case else json.dumps(case["file"])
    path.write_text(text, encoding="utf-8")
    env = {"OPCUA_POLICY_FILE": str(path), **case.get("env", {})}

    # `errorPrefix` cases carry no `error` key, and are refusals.
    if "error" in case and case["error"] is None:
        actual = _settings(parse_policy_config(env))
        for key, expected in case.get("expect", {}).items():
            assert actual[key] == expected, key
        return

    with pytest.raises(ValueError) as refused:
        parse_policy_config(env)
    if "errorPrefix" in case:
        assert str(refused.value).startswith(case["errorPrefix"].replace("{path}", str(path)))
    else:
        assert str(refused.value) == case["error"].replace("{path}", str(path))
