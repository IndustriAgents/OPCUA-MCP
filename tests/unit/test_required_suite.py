"""The required-suite gate itself (#142).

It exists because a release gate once skipped every Alarms & Conditions test and
stayed green. These drive the plugin through a throwaway pytest run and check
the two ways it can be defeated: a skip, and a group that runs nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]

pytest_plugins = ["pytester"]

CONFTEST = """
import required_suite

GROUPS = {
    "alarms": required_suite.Group("test_", lambda item: "alarm" in item.nodeid),
    "core": required_suite.Group("test_", lambda item: "core" in item.nodeid),
    "binaries": required_suite.Group("bins/", lambda item: True),
}

def pytest_configure(config):
    config.pluginmanager.register(
        required_suite.RequiredSuite(required_suite.enabled(), GROUPS), "required-suite"
    )
"""


@pytest.fixture
def suite(pytester, monkeypatch):
    # The inner run is a subprocess, so it finds the plugin by path, not by ours.
    monkeypatch.setenv("PYTHONPATH", str(TESTS_DIR))
    pytester.makeconftest(CONFTEST)
    return pytester


def _run(pytester, monkeypatch, *, required: bool):
    if required:
        monkeypatch.setenv("OPCUA_TESTS_REQUIRED", "1")
    else:
        monkeypatch.delenv("OPCUA_TESTS_REQUIRED", raising=False)
    return pytester.runpytest_subprocess("-p", "no:cacheprovider")


def test_a_skipped_fixture_fails_a_required_run(suite, monkeypatch):
    suite.makepyfile(
        test_core="def test_core(): pass",
        test_alarm="""
import pytest

@pytest.fixture
def alarm_mock():
    pytest.skip("alarms mock not installed")

def test_alarm(alarm_mock): pass
""",
    )
    result = _run(suite, monkeypatch, required=True)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*forbids this skip: alarms mock not installed*"])
    result.stdout.fnmatch_lines(["*required group 'alarms' executed no passing tests*"])


def test_the_same_skip_is_only_reported_locally(suite, monkeypatch):
    suite.makepyfile(
        test_core="def test_core(): pass",
        test_alarm="import pytest\n\ndef test_alarm(): pytest.skip('alarms mock not installed')",
    )
    result = _run(suite, monkeypatch, required=False)
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*alarms*1 skipped*"])


def test_a_group_that_collects_nothing_fails_a_required_run(suite, monkeypatch):
    # No skip at all: the alarm module is simply gone. Zero tests never fail on
    # their own, which is the second way a subsystem disappears from a gate.
    suite.makepyfile(test_core="def test_core(): pass")
    result = _run(suite, monkeypatch, required=True)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*group 'alarms' executed no passing tests (0 collected)*"])


def test_a_complete_run_passes_and_reports_every_group(suite, monkeypatch):
    suite.makepyfile(
        test_core="def test_core(): pass",
        test_alarm="def test_alarm(): pass",
    )
    result = _run(suite, monkeypatch, required=True)
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*alarms*1 passed*", "*core*1 passed*"])


def test_a_group_outside_the_selected_scope_is_not_enforced(suite, monkeypatch):
    # "binaries" lives under bins/, which this run never selected — as when a
    # release job runs only the executables, or only the e2e suite.
    suite.makepyfile(
        test_core="def test_core(): pass",
        test_alarm="def test_alarm(): pass",
    )
    result = _run(suite, monkeypatch, required=True)
    assert result.ret == 0
    assert "binaries" not in result.stdout.str()
