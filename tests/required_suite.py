"""Required-suite mode: a release gate that cannot lose a subsystem to a skip (#142).

Every mock the suite needs is optional locally — a fixture whose dependencies
are not installed skips the tests behind it, which is the right thing on a
laptop. In a gate it is the wrong thing. `publish.yml` never installed the
Alarms & Conditions mock, so every alarm test skipped on every release and the
job stayed green: an entire protocol area was untested at exactly the point
where it mattered, and nothing said so.

Setting ``OPCUA_TESTS_REQUIRED=1`` turns that around, in two ways:

* **No unexplained skips.** A skip whose reason is not in ``ALLOWED_SKIPS`` is
  reported as a failure. This catches every skip site at once — a missing mock,
  an unbuilt Node server, a Node too old for a fixture — rather than relying on
  each one being written to know about the gate.
* **No empty groups.** Each group in ``REQUIRED_GROUPS`` whose part of the
  suite was selected must have at least one test that actually ran and passed.
  A skip is not the only way a subsystem disappears: a renamed fixture or a
  deleted module collects nothing at all, and zero tests never fail.

Either way the run ends with a per-group summary, so what a gate covered is in
the log rather than inferred from a green tick.

Registered from ``conftest.py``. The groups are defined by what a test *uses*
(the mock fixture, the runtime parameter) rather than by file name wherever
possible, because that is what a missing dependency takes away.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import NamedTuple

import pytest

ENV_VAR = "OPCUA_TESTS_REQUIRED"

# Substrings of skip reasons that a required run tolerates. Empty on purpose:
# every skip in the suite today is a missing dependency, and a gate that has one
# is misconfigured. A genuinely platform-specific skip belongs here, with the
# reason it is acceptable, and is still counted in the summary.
ALLOWED_SKIPS: dict[str, str] = {}


def _module(item: pytest.Item) -> str:
    return item.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]


def _runtime(item: pytest.Item) -> str | None:
    """``python`` or ``node`` if the test is parametrised over the runtime."""
    params = getattr(getattr(item, "callspec", None), "params", {})
    for value in params.values():
        if value in ("python", "node"):
            return value
    return None


def _uses(fixture: str) -> Callable[[pytest.Item], bool]:
    return lambda item: fixture in getattr(item, "fixturenames", ())


def _in(*modules: str) -> Callable[[pytest.Item], bool]:
    return lambda item: _module(item) in modules


class Group(NamedTuple):
    """A set of tests a required run must execute.

    ``scope`` is the part of the suite the group lives in — a directory or one
    module. The group is enforced only when the run selected something in that
    scope, so a gate that runs just the executables is not failed for skipping
    the alarms mock, while a deleted alarm module inside a full run still is.
    """

    scope: str
    belongs: Callable[[pytest.Item], bool]


# A test may belong to several groups.
REQUIRED_GROUPS: dict[str, Group] = {
    "unit": Group("unit/", lambda item: True),
    "core (main mock)": Group("e2e/", _uses("opcua_server")),
    "runtime: python": Group("e2e/", lambda item: _runtime(item) == "python"),
    "runtime: node": Group("e2e/", lambda item: _runtime(item) == "node"),
    "security": Group(
        "e2e/",
        lambda item: (
            _uses("secure_opcua_server")(item)
            or _in("test_security_startup.py", "test_policy_e2e.py")(item)
        ),
    ),
    "history": Group("e2e/", _in("test_mcp_e2e.py", "test_event_history_e2e.py")),
    "aggregates": Group("e2e/", _uses("aggregate_opcua_server")),
    "events": Group("e2e/", _in("test_events_e2e.py", "test_event_history_e2e.py")),
    "alarms & conditions": Group("e2e/", _uses("alarm_opcua_server")),
    "resilience": Group("e2e/", _in("test_resilience_e2e.py")),
    "differential parity": Group(
        "e2e/", _in("test_runtime_differential.py", "test_contract_parity.py")
    ),
    "executable: node": Group("smoke/test_binaries.py", lambda item: _runtime(item) == "node"),
    "executable: python": Group("smoke/test_binaries.py", lambda item: _runtime(item) == "python"),
}


def enabled() -> bool:
    return os.environ.get(ENV_VAR) == "1"


def _allowed(reason: str) -> bool:
    return any(fragment in reason for fragment in ALLOWED_SKIPS)


def _skip_reason(report: pytest.TestReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr)


class RequiredSuite:
    """The plugin. One instance per session; state lives on it, not the module."""

    def __init__(
        self,
        required: bool,
        groups: dict[str, Group] | None = None,
    ) -> None:
        self.required = required
        self.definitions = REQUIRED_GROUPS if groups is None else groups
        self.groups: dict[str, set[str]] = defaultdict(set)
        self.scopes: set[str] = set()
        self.outcomes: dict[str, str] = {}
        self.problems: list[str] = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        # trylast: -k and -m have already deselected, so this sees what will run.
        for item in items:
            for group, (scope, belongs) in self.definitions.items():
                if item.nodeid.startswith(scope):
                    self.scopes.add(scope)
                    if belongs(item):
                        self.groups[group].add(item.nodeid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item, call):
        outcome = yield
        report: pytest.TestReport = outcome.get_result()
        if report.skipped and not hasattr(report, "wasxfail"):
            reason = _skip_reason(report)
            if self.required and not _allowed(reason):
                report.outcome = "failed"
                report.longrepr = (
                    f"{ENV_VAR}=1 forbids this skip: {reason}\n"
                    "A required run must execute every test group. Install the missing "
                    "dependency, or add the reason to ALLOWED_SKIPS in "
                    "tests/required_suite.py with a justification."
                )

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # A test's outcome is its worst phase: a setup skip or a teardown error
        # both have to win over a passing call.
        if report.when == "call" or report.outcome != "passed":
            previous = self.outcomes.get(report.nodeid)
            if previous in (None, "passed") or report.outcome == "failed":
                self.outcomes[report.nodeid] = report.outcome

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if not self.required:
            return
        for group, (scope, _) in self.definitions.items():
            if scope not in self.scopes:
                continue
            passed = sum(1 for n in self.groups.get(group, ()) if self.outcomes.get(n) == "passed")
            if passed == 0:
                self.problems.append(
                    f"required group {group!r} executed no passing tests "
                    f"({len(self.groups.get(group, ()))} collected)"
                )
        if self.problems and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if not self.groups:
            return
        tr = terminalreporter
        mode = f"required by {ENV_VAR}=1" if self.required else f"set {ENV_VAR}=1 to enforce"
        tr.section(f"test groups ({mode})")
        for group, (scope, _) in self.definitions.items():
            if scope not in self.scopes:
                continue
            counts = Counter(self.outcomes.get(n, "not run") for n in self.groups.get(group, ()))
            detail = ", ".join(f"{counts[k]} {k}" for k in sorted(counts)) or "none collected"
            tr.write_line(f"  {group:<22} {detail}")
        for problem in self.problems:
            tr.write_line(f"ERROR: {problem}", red=True)
