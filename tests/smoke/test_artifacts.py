"""Smoke tests against the *packaged* artifacts, not the source tree.

Everything else in this suite runs from the repo, where relative paths happen to
resolve. Users get a tarball or a wheel, where they may not. That gap is not
hypothetical: `opcua-mcp-server` shipped a release whose Python wheel raised
`FileNotFoundError` on import because the shared tool contract resolved via
`Path(__file__).parents[2]`, which is only the repo root in a source checkout.

So these tests build the real artifacts, install them somewhere isolated, and
drive the installed entry point over MCP:

  * npm: `npm pack` → install the tarball into a scratch project → run the
    ``node_modules/.bin`` shim. The shim is a **symlink**, which is deliberate —
    it is what `npx` invokes, and it is the case most likely to break an
    entry-point guard that compares `import.meta.url` to `process.argv[1]`.
  * Python: `uv build` → install the wheel into a fresh venv → run the console
    script with a cwd *outside* the repo, so a path that only resolves in the
    source tree cannot accidentally pass.

Marked ``smoke``; they are slow (npm install + venv creation) and run as their
own CI job. Deselect with ``-m "not smoke"``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest
from conftest import ROOT
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.smoke

# Populated by the wheel_venv fixture so the sdist test can reuse the build.
_DIST_DIRS: list = []

NODE_PKG_DIR = ROOT / "packages" / "server-node"
PY_PKG_DIR = ROOT / "packages" / "server-python"

# Tools every build must advertise regardless of server capabilities. Capability
# -gated tools (history/aggregate) are covered by the e2e suite instead.
CORE_TOOLS = {
    "read_opcua_nodes",
    "browse_opcua_nodes",
    "write_opcua_nodes",
    "call_opcua_method",
    "get_server_status",
    "subscribe_opcua_nodes",
    "list_subscriptions",
    "unsubscribe_opcua_nodes",
    "subscribe_events",
    "read_events",
    "list_active_alarms",
    "acknowledge_alarm",
}


def _run(cmd, cwd, **kw):
    """Run a command, surfacing stdout/stderr in the failure message."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600, **kw)
    if proc.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(map(str, cmd))}\n"
            f"cwd: {cwd}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


async def _list_tools(params: StdioServerParameters) -> set[str]:
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return {t.name for t in (await session.list_tools()).tools}


def node_tool(name: str) -> str:
    """The full path to a Node CLI, or skip.

    Resolved rather than passed as a bare name: on Windows `npm` is `npm.cmd`,
    and `subprocess.run(["npm", ...])` without a shell fails with
    `FileNotFoundError: [WinError 2]`. `shutil.which` already knows this; the
    fixtures were asking it and then discarding the answer.
    """
    resolved = shutil.which(name)
    if resolved is None:
        pytest.skip(f"{name} not available")
    return resolved


@pytest.fixture(scope="module")
def npm_install(tmp_path_factory):
    """Pack the npm tarball and install it into a scratch project."""
    npm = node_tool("npm")

    staging = tmp_path_factory.mktemp("npm-pack")
    out = _run([npm, "pack", "--pack-destination", str(staging)], cwd=NODE_PKG_DIR)
    tarball = staging / out.stdout.strip().splitlines()[-1]
    assert tarball.is_file(), f"npm pack did not produce {tarball}"

    project = tmp_path_factory.mktemp("npm-consumer")
    _run([npm, "init", "-y"], cwd=project)
    _run([npm, "install", str(tarball)], cwd=project)
    return project


def test_npm_tarball_contains_runtime_assets(npm_install):
    """The published package must carry everything index.js reads at runtime."""
    pkg = npm_install / "node_modules" / "opcua-mcp-server"
    for asset in (
        "build/index.js",
        "build/contract.json",
        "build/config.json",
        "build/version.json",
    ):
        assert (pkg / asset).is_file(), f"{asset} missing from the npm package"


def test_npm_bin_shims_are_installed(npm_install):
    """Both documented commands must exist as executable shims."""
    bindir = npm_install / "node_modules" / ".bin"
    for name in ("opcua-mcp-server", "opcua-mcp"):
        shim = bindir / name
        assert shim.exists(), f"bin shim {name} not installed"


async def test_npm_installed_server_lists_tools(npm_install, opcua_server):
    """The installed shim must start and serve tools/list over MCP.

    Runs the ``.bin`` symlink from a cwd outside the repo — the exact shape of
    invocation `npx` uses.
    """
    shim = npm_install / "node_modules" / ".bin" / "opcua-mcp-server"
    params = StdioServerParameters(
        command=str(shim),
        args=[],
        env={
            **os.environ,
            "OPCUA_SERVER_URL": opcua_server,
            "OPCUA_PROFILE": "full",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
        },
        cwd=str(npm_install),
    )
    assert await _list_tools(params) >= CORE_TOOLS


@pytest.fixture(scope="module")
def wheel_venv(tmp_path_factory):
    """Build the Python wheel and install it into a fresh, isolated venv."""
    if shutil.which("uv") is None:
        pytest.skip("uv not available")

    dist = tmp_path_factory.mktemp("wheel")
    _DIST_DIRS.append(dist)
    # Plain `uv build`, not `--wheel`: it builds the sdist and then the wheel
    # *from that sdist*, which is what PyPI publishing and `pip install <sdist>`
    # do. Building only the wheel skips that path entirely — and that is how a
    # release shipped with a `force-include` that resolved in a checkout but not
    # in an sdist, failing the publish job.
    _run(["uv", "build", "--out-dir", str(dist)], cwd=PY_PKG_DIR)
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    # `uv venv` rather than stdlib `venv`: uv-managed interpreters ship without a
    # working `ensurepip`, so `venv.create(with_pip=True)` aborts on them.
    env_dir = tmp_path_factory.mktemp("wheel-venv") / "venv"
    _run(["uv", "venv", str(env_dir)], cwd=dist)
    bindir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    python = bindir / ("python.exe" if sys.platform == "win32" else "python")
    _run(["uv", "pip", "install", "--python", str(python), str(wheels[0])], cwd=dist)
    return bindir


def test_wheel_imports_outside_source_tree(wheel_venv, tmp_path):
    """Importing the installed module must not depend on the repo layout.

    Regression guard for the shipped `FileNotFoundError`: the contract was
    resolved relative to the source checkout, so the wheel worked in-repo and
    failed everywhere else. `cwd` is deliberately outside the repo.
    """
    python = wheel_venv / ("python.exe" if sys.platform == "win32" else "python")
    proc = _run(
        [
            str(python),
            "-c",
            "import opcua_mcp_server as m; import json; print(json.dumps(sorted(m.DESC)))",
        ],
        cwd=tmp_path,
    )
    assert set(json.loads(proc.stdout)) >= CORE_TOOLS


def test_wheel_ships_the_config_schema(wheel_venv, tmp_path):
    """The configuration schema must load from an installed wheel, outside any
    checkout, for the same reason the tool contract must: `--install` (#135)
    reads it from wherever the package was installed."""
    python = wheel_venv / ("python.exe" if sys.platform == "win32" else "python")
    proc = _run(
        [
            str(python),
            "-c",
            "from opcua_mcp_server.contract import load_config_schema as load; "
            "import json; print(json.dumps(load()))",
        ],
        cwd=tmp_path,
    )
    canonical = json.loads((ROOT / "contract" / "config.json").read_text(encoding="utf-8"))
    assert json.loads(proc.stdout) == canonical


def test_sdist_is_self_contained(wheel_venv, tmp_path_factory):
    """A wheel must be buildable from the sdist alone, outside any checkout.

    Regression guard for the failed 0.2.0 PyPI publish: the contract was
    force-included from `../../contract/tools.json`, a path that exists in the
    repo but can never exist inside an sdist.
    """
    dist = next(iter(_DIST_DIRS))
    sdists = list(dist.glob("*.tar.gz"))
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"

    # Unpack somewhere with no repo above it, then build a wheel from it.
    workdir = tmp_path_factory.mktemp("sdist-only")
    with tarfile.open(sdists[0]) as tar:
        # `filter` is only available from 3.12 (and 3.10/3.11 point releases);
        # the floor here is 3.10, so pass it only where it certainly exists.
        extra = {"filter": "data"} if sys.version_info >= (3, 12) else {}
        tar.extractall(workdir, **extra)
    unpacked = next(p for p in workdir.iterdir() if p.is_dir())

    out = workdir / "out"
    _run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=unpacked)
    built = list(out.glob("*.whl"))
    assert len(built) == 1, f"expected one wheel from the sdist, got {built}"

    with zipfile.ZipFile(built[0]) as zf:
        for staged in ("opcua_mcp_server/tools.json", "opcua_mcp_server/config.json"):
            assert staged in zf.namelist(), f"wheel built from the sdist is missing {staged}"


def test_wheel_does_not_pollute_site_packages(wheel_venv):
    """The distribution must install exactly one importable top-level name.

    The contract used to be force-included at the *wheel root*, so installing
    dropped two top-level files into site-packages and the contract needed a
    namespaced filename to avoid colliding with other distributions. It now ships
    inside the package.
    """
    # Windows puts it at `Lib/site-packages`; POSIX at `lib/pythonX.Y/site-packages`.
    candidates = [
        *(wheel_venv.parent / "lib").glob("python*/site-packages"),
        wheel_venv.parent / "Lib" / "site-packages",
    ]
    site_packages = next(path for path in candidates if path.is_dir())
    record = next(site_packages.glob("opcua_mcp_server-*.dist-info/RECORD")).read_text(
        encoding="utf-8"
    )

    top_level = {line.split("/")[0] for line in record.splitlines() if line.strip()}
    # Drop metadata and the console script, which RECORD lists as ../../../bin/...
    importable = {t for t in top_level if not t.endswith(".dist-info") and t != ".."}

    assert importable == {"opcua_mcp_server"}, (
        f"wheel installs unexpected top-level entries: {sorted(importable)}"
    )


def test_wheel_console_script_installed(wheel_venv):
    script = wheel_venv / (
        "opcua-mcp-server.exe" if sys.platform == "win32" else "opcua-mcp-server"
    )
    assert script.exists(), "console script `opcua-mcp-server` not installed by the wheel"


async def test_wheel_installed_server_lists_tools(wheel_venv, opcua_server, tmp_path):
    """The installed console script must start and serve tools/list over MCP."""
    script = wheel_venv / (
        "opcua-mcp-server.exe" if sys.platform == "win32" else "opcua-mcp-server"
    )
    params = StdioServerParameters(
        command=str(script),
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


# --- MCP bundle (.mcpb) --------------------------------------------------------
# The `.mcpb` is the download-and-double-click artifact for Claude Desktop, and
# unlike the npm package it carries no node_modules: the whole dependency tree is
# bundled into one file, with the tool contract inlined at build time. That is a
# genuinely different build path from everything above, and node-opcua is not a
# library that bundles by accident — it needs `require`, `__filename` and
# `__dirname` handed to it. So these tests pack the real bundle, unpack it, and
# drive the server inside it against a live OPC UA server.


@pytest.fixture(scope="module")
def packed_mcpb(tmp_path_factory):
    """Build the real `.mcpb` and unpack it, yielding the extracted directory."""
    npm = node_tool("npm")
    if not (NODE_PKG_DIR / "node_modules").is_dir():
        pytest.skip("Node dependencies not installed — run `npm ci` in packages/server-node")

    _run([npm, "run", "build:mcpb"], cwd=NODE_PKG_DIR)
    bundles = list((NODE_PKG_DIR / "dist").glob("*.mcpb"))
    assert len(bundles) == 1, f"expected exactly one .mcpb, got {bundles}"

    unpacked = tmp_path_factory.mktemp("mcpb") / "extension"
    unpack = [node_tool("npx"), "--no-install", "mcpb", "unpack", str(bundles[0]), str(unpacked)]
    _run(unpack, cwd=NODE_PKG_DIR)
    return unpacked


def test_mcpb_manifest_matches_the_package_version(packed_mcpb):
    """`build-mcpb.mjs` stamps the version from package.json, so a release that
    forgets to touch mcpb/manifest.json still ships a correctly labelled bundle."""
    manifest = json.loads((packed_mcpb / "manifest.json").read_text(encoding="utf-8"))
    package = json.loads((NODE_PKG_DIR / "package.json").read_text(encoding="utf-8"))
    assert manifest["version"] == package["version"]


def test_mcpb_is_self_contained(packed_mcpb):
    """No node_modules, and the declared entry point is really in the bundle.

    Claude Desktop runs the entry point as-is; it does not install anything. A
    bundle that expected its dependencies to be present would fail at startup on
    a user's machine and nowhere else.
    """
    manifest = json.loads((packed_mcpb / "manifest.json").read_text(encoding="utf-8"))
    entry = packed_mcpb / manifest["server"]["entry_point"]
    assert entry.is_file(), f"entry point {manifest['server']['entry_point']} missing"
    assert not list(packed_mcpb.rglob("node_modules"))


def test_mcpb_exposes_the_endpoint_as_user_config(packed_mcpb):
    """The endpoint must be a `user_config` field wired into the server's env.

    This is the entire reason the bundle removes the need to edit JSON: Claude
    Desktop renders the field as a form and substitutes the answer here. If the
    two halves stop matching, the server silently starts on the default endpoint.
    """
    manifest = json.loads((packed_mcpb / "manifest.json").read_text(encoding="utf-8"))
    assert "opcua_server_url" in manifest["user_config"]
    assert manifest["user_config"]["opcua_server_url"]["required"] is True
    env = manifest["server"]["mcp_config"]["env"]
    assert env["OPCUA_SERVER_URL"] == "${user_config.opcua_server_url}"


def test_mcpb_exposes_every_applicable_setting(packed_mcpb):
    """The bundle must be able to express every setting the schema offers it.

    Claude Desktop passes the server exactly the env this manifest declares and
    nothing else, so a variable missing here is one a bundle user can never set.
    This test was once named for "every security setting" while pinning six of
    them by hand, and passed while server-certificate pinning, X.509 user login
    and the audit file were all unreachable (#133). The expected set now comes
    from `contract/config.json`, so it cannot be a smaller list than the truth.

    Checked on the *packed* manifest: `tests/unit/test_config_schema.py` checks
    the committed one, and this catches a build step that loses what it had.
    """
    schema = json.loads((ROOT / "contract" / "config.json").read_text(encoding="utf-8"))
    expected = [s for s in schema["settings"] if "mcpb" in s["surfaces"]]
    manifest = json.loads((packed_mcpb / "manifest.json").read_text(encoding="utf-8"))
    env = manifest["server"]["mcp_config"]["env"]
    user_config = manifest["user_config"]

    missing = sorted({s["env"] for s in expected} - set(env))
    assert not missing, f"not settable from the bundle: {missing}"

    for setting in expected:
        # Wired to a form field, not hardcoded, and masked if it must be.
        field = f"opcua_{setting['key']}"
        assert env[setting["env"]] == "${user_config." + field + "}", setting["env"]
        assert field in user_config, f"{setting['env']} points at a missing field {field}"
        assert user_config[field].get("sensitive", False) is setting["sensitive"], (
            f"{field} must be marked sensitive exactly when the schema says so"
        )
        if setting["type"] == "boolean":
            assert user_config[field]["default"] is setting["default"], field

    assert user_config["opcua_profile"]["default"] == "observe"


async def test_mcpb_server_starts_with_every_optional_setting_blank(packed_mcpb, opcua_server):
    """Unset optional fields arrive as empty strings, and must mean "not configured".

    This is how the bundle runs for anyone who only fills in the endpoint — the
    common case. It once failed outright: an empty `OPCUA_PASSWORD` was read as
    half a credential and the server exited with a configuration error before
    serving anything.
    """
    manifest = json.loads((packed_mcpb / "manifest.json").read_text(encoding="utf-8"))
    blank = {name: "" for name in manifest["server"]["mcp_config"]["env"]}
    params = StdioServerParameters(
        command="node",
        args=[str(packed_mcpb / manifest["server"]["entry_point"])],
        env={
            **os.environ,
            **blank,
            "OPCUA_SERVER_URL": opcua_server,
            "OPCUA_PROFILE": "full",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
        },
        cwd=str(packed_mcpb),
    )
    assert await _list_tools(params) >= CORE_TOOLS


async def test_mcpb_server_lists_tools(packed_mcpb, opcua_server):
    """The bundled server must actually start and serve tools/list over MCP.

    The point of doing this against the *unpacked bundle* rather than the source:
    bundling inlines the contract and rewrites node-opcua's CommonJS requires, and
    a mistake in either shows up only here.
    """
    manifest = json.loads((packed_mcpb / "manifest.json").read_text(encoding="utf-8"))
    params = StdioServerParameters(
        command="node",
        args=[str(packed_mcpb / manifest["server"]["entry_point"])],
        env={
            **os.environ,
            "OPCUA_SERVER_URL": opcua_server,
            "OPCUA_PROFILE": "full",
            "OPCUA_ALLOW_INSECURE_CONTROL": "true",
        },
        cwd=str(packed_mcpb),
    )
    assert await _list_tools(params) >= CORE_TOOLS
