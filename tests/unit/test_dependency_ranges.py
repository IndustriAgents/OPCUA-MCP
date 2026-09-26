"""The published packages' dependency ranges keep the shape the policy promises.

docs/dependency-policy.md (#150) says every direct runtime dependency of both
published packages has a floor CI tests and a ceiling below the next major, so a
fresh `pip install` or `npm install -g` cannot resolve a major nobody here has
run. That is a property of four lines of TOML and three of JSON, which is exactly
the kind of thing a well-meant edit breaks without anyone noticing — so it is
asserted here rather than left to review, together with the policy's table,
which is only worth reading while it matches the manifests.
"""

from __future__ import annotations

import json
import re
import sys

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 only
    import tomli as tomllib

from conftest import ROOT

NODE_PKG = ROOT / "packages" / "server-node" / "package.json"
PYTHON_PYPROJECT = ROOT / "packages" / "server-python" / "pyproject.toml"
TESTS_PYPROJECT = ROOT / "tests" / "pyproject.toml"
MOCK_PYPROJECT = ROOT / "packages" / "mock-server" / "pyproject.toml"
POLICY = ROOT / "docs" / "dependency-policy.md"

# The same rule scripts/pin-dependency-floors.mjs applies: a caret or tilde on a
# full version, and nothing else. The script refuses anything this rejects.
NODE_BOUNDED_RANGE = re.compile(r"^[\^~]\d+\.\d+\.\d+$")


def _python_dependencies(pyproject) -> list[str]:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]


def _node_dependencies() -> dict[str, str]:
    return json.loads(NODE_PKG.read_text(encoding="utf-8"))["dependencies"]


def _split(requirement: str) -> tuple[str, str]:
    """`mcp[cli]>=2.2.0,<3` -> (`mcp[cli]`, `>=2.2.0,<3`), as written."""
    name = re.match(r"^[A-Za-z0-9_.\-]+(\[[^\]]+\])?", requirement).group(0)
    return name, requirement[len(name) :].split(";")[0].strip()


def _next_major(version: Version) -> Version:
    # Below 1.0 the minor is the breaking boundary, as it is for npm's caret.
    if version.major == 0:
        return Version(f"0.{version.minor + 1}")
    return Version(f"{version.major + 1}")


@pytest.mark.parametrize("requirement", _python_dependencies(PYTHON_PYPROJECT))
def test_every_python_runtime_dependency_has_a_floor_and_a_ceiling(requirement):
    specifiers = {
        spec.operator: Version(spec.version) for spec in Requirement(requirement).specifier
    }
    assert set(specifiers) == {">=", "<"}, (
        f"{requirement!r}: a runtime dependency needs exactly `>=floor,<ceiling` — "
        "see docs/dependency-policy.md#range-rules"
    )
    floor, ceiling = specifiers[">="], specifiers["<"]
    # At most one major wide. A ceiling two majors up would let a fresh install
    # resolve one that CI never ran.
    assert floor < ceiling <= _next_major(floor), (
        f"{requirement!r}: the ceiling must be no higher than the next major of the floor "
        f"({_next_major(floor)})"
    )


@pytest.mark.parametrize("name,range_", sorted(_node_dependencies().items()))
def test_every_node_runtime_dependency_is_caret_or_tilde_bounded(name, range_):
    assert NODE_BOUNDED_RANGE.match(range_), (
        f"{name}@{range_}: a runtime dependency needs `^x.y.z` or `~x.y.z` — "
        "see docs/dependency-policy.md#range-rules"
    )


@pytest.mark.parametrize("pyproject", [TESTS_PYPROJECT, MOCK_PYPROJECT], ids=["tests", "mock"])
def test_the_workspace_tests_the_ranges_the_package_ships(pyproject):
    """The test suite and the mock declare some of the server's dependencies
    again. Where they do, the range has to be the server's, or the lowest job
    would resolve — and pass on — a floor the published package does not have."""
    shipped = dict(_split(r) for r in _python_dependencies(PYTHON_PYPROJECT))
    for requirement in _python_dependencies(pyproject):
        name, range_ = _split(requirement)
        if name in shipped:
            assert range_ == shipped[name], (
                f"{pyproject.relative_to(ROOT)} declares {name}{range_}, "
                f"the server ships {name}{shipped[name]}"
            )


def test_the_policy_lists_every_runtime_dependency_with_its_range():
    rows = [
        line for line in POLICY.read_text(encoding="utf-8").splitlines() if line.startswith("|")
    ]
    declared = [_split(r) for r in _python_dependencies(PYTHON_PYPROJECT)]
    declared += list(_node_dependencies().items())
    for name, range_ in declared:
        assert any(f"| `{name}` | `{range_}` |" in row for row in rows), (
            f"docs/dependency-policy.md has no table row for `{name}` `{range_}`"
        )
    # And nothing the manifests no longer declare, which is how a removed
    # dependency would linger in the policy.
    listed = {
        match.group(1) for row in rows if (match := re.match(r"^\| `([^`]+)` \| `[^`]+` \|", row))
    }
    assert listed == {name for name, _ in declared}
