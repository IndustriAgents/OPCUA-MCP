"""Shared exact CLI grammar; parsing has no connection or configuration effects."""

from __future__ import annotations

import json

import pytest
from conftest import ROOT
from opcua_mcp_server.install import parse_args

FIXTURE = json.loads((ROOT / "tests/fixtures/cli-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_shared_cli_grammar(case):
    action = parse_args(case["argv"], FIXTURE["defaultUrl"])
    assert action.kind == case["kind"]
    if "message" in case:
        assert action.message == case["message"]
    if "settings" in case:
        assert action.options is not None
        assert action.options.settings == case["settings"]
