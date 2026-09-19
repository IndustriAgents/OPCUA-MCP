"""Smoke tests for the single-file executables.

These are the artifacts for a machine with no Node and no Python — the normal
case on an air-gapped plant network. Both embed their own interpreter, which
means they exercise a code path nothing else in the suite does:

  * the Node build inlines the tool contract and rewrites node-opcua's CommonJS
    requires, then injects the result into a copy of the ``node`` binary;
  * the Python build freezes the interpreter and has to be told where the
    contract went, because ``Path(__file__).parents[4]`` inside a frozen app
    points at nothing.

Either can break while every other test stays green, and the failure would land
on a user rather than in CI. So the binaries are built here and driven over MCP.

Marked ``smoke``; they build a ~110 MB (Node) and ~30 MB (Python) executable, so
they run as their own CI job. Deselect with ``-m "not smoke"``.

Neither can be cross-compiled, so this only ever covers the platform it runs on;
the release workflow builds and checks the other two.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT
from mcp import StdioServerParameters
from test_artifacts import CORE_TOOLS, NODE_PKG_DIR, PY_PKG_DIR, _list_tools, _run

pytestmark = pytest.mark.smoke

EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

# Both build scripts name their output `opcua-mcp-server-<runtime>-<platform>-<arch>`,
# with the architecture spelled the way Node's `process.arch` spells it.
ARCH = {"x86_64": "x64", "amd64": "x64", "AMD64": "x64", "aarch64": "arm64", "arm64": "arm64"}[
    platform.machine()
]
PLATFORM = f"{sys.platform}-{ARCH}"


def _node_major() -> int:
    out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=60)
    return int(out.stdout.strip().lstrip("v").split(".")[0])


@pytest.fixture(scope="module")
def node_binary():
    """Build the Node single-file executable."""
    if shutil.which("npm") is None:
        pytest.skip("npm not available")
    if not (NODE_PKG_DIR / "node_modules").is_dir():
        pytest.skip("Node dependencies not installed — run `npm ci` in packages/server-node")
    if _node_major() < 20:
        # A build-time floor only: the server itself still supports Node 18.
        pytest.skip(f"single-file executables need Node 20+ to build; found Node {_node_major()}")

    _run(["npm", "run", "build:sea"], cwd=NODE_PKG_DIR)
    binary = NODE_PKG_DIR / "dist" / f"opcua-mcp-server-node-{PLATFORM}{EXE_SUFFIX}"
    assert binary.is_file(), f"expected {binary} after build:sea"
    return binary


@pytest.fixture(scope="module")
def python_binary():
    """Build the Python single-file executable with PyInstaller."""
    if shutil.which("uv") is None:
        pytest.skip("uv not available")

    probe = subprocess.run(
        ["uv", "run", "--group", "packaging", "pyinstaller", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if probe.returncode != 0:
        pytest.skip("PyInstaller not available — run `uv sync --all-packages --group packaging`")

    _run(
        [
            "uv",
            "run",
            "--group",
            "packaging",
            "pyinstaller",
            "--noconfirm",
            "--distpath",
            str(PY_PKG_DIR / "dist"),
            "--workpath",
            str(PY_PKG_DIR / "build-pyinstaller"),
            str(PY_PKG_DIR / "packaging" / "opcua-mcp-server.spec"),
        ],
        cwd=ROOT,
    )
    binary = PY_PKG_DIR / "dist" / f"opcua-mcp-server-python-{PLATFORM}{EXE_SUFFIX}"
    assert binary.is_file(), f"expected {binary} after the PyInstaller build"
    return binary


@pytest.fixture(params=["node", "python"])
def binary(request):
    """Each test runs against both runtimes' executables."""
    return request.getfixturevalue(f"{request.param}_binary")


def _scratch_home(tmp_path) -> dict:
    """An environment whose home directory is disposable.

    Both runtimes resolve the Claude Desktop config path from the home directory,
    and these tests must not go near a developer's real one.
    """
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "AppData" / "Roaming"),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
    }


def test_binary_reports_the_manifest_version(binary):
    """A frozen build must still know its own version.

    Both runtimes read it from packaging metadata that only exists in a normal
    install, so this is exactly the kind of thing freezing breaks.
    """
    proc = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    expected = json.loads((NODE_PKG_DIR / "package.json").read_text(encoding="utf-8"))["version"]
    assert proc.stdout.strip() == expected


def test_binary_install_records_itself_and_nothing_else(binary, tmp_path):
    """`--install` from a single-file build must name the binary alone.

    A frozen executable is not an interpreter: an entry that handed it a script
    path or ``-m opcua_mcp_server`` would be written happily and then fail every
    time Claude Desktop tried to start it.
    """
    proc = subprocess.run(
        [str(binary), "--install", "claude-desktop", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=120,
        env=_scratch_home(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr

    entry = json.loads(proc.stdout[proc.stdout.index("{") :])["mcpServers"]["opcua"]
    assert entry["command"] == str(binary)
    assert "args" not in entry, f"a frozen build cannot accept {entry.get('args')}"


async def test_binary_lists_tools(binary, opcua_server, tmp_path):
    """The executable must serve MCP with no runtime on the machine.

    Runs from a cwd outside the repo, since a frozen app that quietly resolved
    the contract relative to the source tree would otherwise pass.
    """
    params = StdioServerParameters(
        command=str(binary),
        args=[],
        env={
            **os.environ,
            "OPCUA_SERVER_URL": opcua_server,
            "OPCUA_PROFILE": "full",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
        },
        cwd=str(tmp_path),
    )
    assert await _list_tools(params) >= CORE_TOOLS
