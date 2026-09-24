"""The deprecation allowlist, and the two mechanisms that read it (#150).

The allowlist is the one place an upstream deprecation warning may be excused,
so every entry has to say who owns it, which issue tracks it and when it goes.
The rest checks that the mechanisms actually bite: that a deprecation is an
error in this very process, and that the stderr scanner the e2e test relies on
tells an upstream warning from one raised in our own code.
"""

from __future__ import annotations

import re
import warnings

import pytest
from deprecations import (
    REQUIRED_FIELDS,
    load_allowlist,
    module_of,
    unexpected_deprecations,
)

ENTRIES = load_allowlist()
OUR_MODULES = ("opcua_mcp_server", "opcua_mcp_server.server", "opcua_local_server")

UTCNOW = (
    "datetime.datetime.utcnow() is deprecated and scheduled for removal in a future "
    "version. Use timezone-aware objects to represent datetimes in UTC: "
    "datetime.datetime.now(datetime.UTC)."
)


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: f"{e['runtime']}:{e['package']}")
def test_every_entry_names_an_issue_an_owner_and_an_exit(entry):
    for field in REQUIRED_FIELDS:
        assert str(entry.get(field, "")).strip(), f"allowlist entry has no {field!r}: {entry}"
    assert entry["runtime"] in {"python", "node"}
    assert re.fullmatch(r"#\d+", entry["issue"]), entry["issue"]
    assert entry["owner"].startswith("@"), entry["owner"]


@pytest.mark.parametrize(
    "entry", [e for e in ENTRIES if e["runtime"] == "python"], ids=lambda e: e["package"]
)
def test_python_entries_cannot_excuse_our_own_code(entry):
    assert entry.get("module"), "a python entry must be scoped to the upstream module"
    # `:` separates the fields of a filterwarnings line, and these are passed in raw.
    assert ":" not in entry["module"] and ":" not in entry["message"]
    re.compile(entry["module"])
    re.compile(entry["message"])
    for ours in OUR_MODULES:
        assert not re.match(entry["module"], ours), f"{entry['module']!r} matches {ours}"


def test_a_deprecation_is_an_error_in_this_process():
    with pytest.raises(DeprecationWarning):
        warnings.warn("something of ours is deprecated", DeprecationWarning, stacklevel=1)
    with pytest.raises(PendingDeprecationWarning):
        warnings.warn("soon to be deprecated", PendingDeprecationWarning, stacklevel=1)


def test_an_allowlisted_upstream_warning_is_not():
    warnings.warn_explicit(UTCNOW, DeprecationWarning, "uaprotocol_auto.py", 1, module="opcua.ua")


def test_the_same_warning_from_our_own_code_is():
    with pytest.raises(DeprecationWarning):
        warnings.warn_explicit(
            UTCNOW, DeprecationWarning, "server.py", 1, module="opcua_mcp_server.server"
        )


@pytest.mark.parametrize(
    "path,module",
    [
        (
            "/x/.venv/lib/python3.13/site-packages/opcua/ua/uaprotocol_auto.py",
            "opcua.ua.uaprotocol_auto",
        ),
        ("/repo/packages/server-python/src/opcua_mcp_server/server.py", "opcua_mcp_server.server"),
        (r"C:\x\.venv\Lib\site-packages\opcua\__init__.py", "opcua"),
        ("/somewhere/script.py", "script"),
    ],
)
def test_module_of_names_what_pytest_would(path, module):
    assert module_of(path) == module


def test_the_stderr_scanner_excuses_only_what_the_allowlist_names():
    upstream = f"/v/site-packages/opcua/ua/uaprotocol_auto.py:6034: DeprecationWarning: {UTCNOW}"
    ours = f"/r/src/opcua_mcp_server/server.py:12: DeprecationWarning: {UTCNOW}"
    pending = f"/v/site-packages/opcua/ua/x.py:1: PendingDeprecationWarning: {UTCNOW}"
    node_opcua = (
        "14:23:32.208Z :opcua_client_impl :309   Warning: endpoint_must_exist is now "
        "deprecated, use endpointMustExist instead"
    )
    node_runtime = "(node:4242) [DEP0005] DeprecationWarning: Buffer() is deprecated"
    log = "\n".join([upstream, "    self.Timestamp = datetime.utcnow()", ours, pending, "ok"])

    assert unexpected_deprecations(log, "python") == [ours, pending]
    assert unexpected_deprecations(f"{node_opcua}\n{node_runtime}", "node") == [
        node_opcua,
        node_runtime,
    ]
    # A python allowlist entry says nothing about the Node runtime.
    assert unexpected_deprecations(upstream, "node") == [upstream]
