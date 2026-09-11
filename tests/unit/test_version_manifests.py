"""Version-parity checks that need no running server.

The version used to be hardcoded in four places (`src/index.ts`, `package.json`,
and both `pyproject.toml`s), which is exactly the kind of thing that silently
drifts across a release. These are the static halves of that guard; the
handshake half lives in e2e/test_version_parity.py.

The packages in this repo are released as a unit, so all the manifests are
expected to carry the same version.
"""

from __future__ import annotations

import json
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 only
    import tomli as tomllib

from conftest import ROOT

NODE_PKG = ROOT / "packages" / "server-node" / "package.json"
PYTHON_PYPROJECT = ROOT / "packages" / "server-python" / "pyproject.toml"
MOCK_PYPROJECT = ROOT / "packages" / "mock-server" / "pyproject.toml"
MCPB_MANIFEST = ROOT / "packages" / "server-node" / "mcpb" / "manifest.json"
NODE_LOCKFILE = ROOT / "packages" / "server-node" / "package-lock.json"


def _node_version() -> str:
    return json.loads(NODE_PKG.read_text())["version"]


def _py_version(pyproject) -> str:
    return tomllib.loads(pyproject.read_text())["project"]["version"]


def test_manifests_agree_on_version():
    """Every package manifest carries the same version (released as a unit).

    The MCP bundle manifest is in here because `mcpb validate` runs against the
    checked-in file, so it has to hold a real version rather than a placeholder —
    which means it can drift. (`scripts/build-mcpb.mjs` stamps the version from
    package.json on the way into the bundle, so a release that forgets this file
    still ships a correct `.mcpb`; this keeps the source honest.)
    """
    versions = {
        "server-node/package.json": _node_version(),
        "server-python/pyproject.toml": _py_version(PYTHON_PYPROJECT),
        "mock-server/pyproject.toml": _py_version(MOCK_PYPROJECT),
        "server-node/mcpb/manifest.json": json.loads(MCPB_MANIFEST.read_text())["version"],
    }
    assert len(set(versions.values())) == 1, f"version drift across manifests: {versions}"


def test_no_hardcoded_version_in_node_source():
    """No Node module may re-declare a version literal.

    Regression guard: `src/index.ts` used to hardcode `version: "0.1.2"` next to
    package.json, so the server advertised a stale version after every release.
    Scans every module, not just index.ts, since the source is now split.
    """
    offenders = [
        path.name
        for path in sorted((ROOT / "packages" / "server-node" / "src").glob("*.ts"))
        if 'version: "' in path.read_text()
    ]
    assert not offenders, f"hardcoded version literal in: {offenders}"


def test_the_npm_lockfile_records_the_package_version():
    """`package-lock.json` carries the root version twice, and a bump misses both.

    npm writes it at the top level and again under `packages[""]`. Neither breaks
    the build when stale, which is exactly the problem: the released tarball
    carries inconsistent metadata, and the next unrelated `npm install` quietly
    produces a version diff nobody asked for. Regenerate with
    `npm install --package-lock-only` rather than editing by hand.
    """
    lock = json.loads(NODE_LOCKFILE.read_text())
    expected = _node_version()
    assert lock["version"] == expected, "package-lock.json top-level version is stale"
    assert lock["packages"][""]["version"] == expected, 'packages[""] version is stale'
