# PyInstaller spec for the downloadable single-file executable.
#
# One download, no Python, no pip, no network — the counterpart of the Node
# runtime's `npm run build:sea`, for the case the `.mcpb` does not cover: an MCP
# client other than Claude Desktop, or a machine with no runtime installed and no
# way to get one, which on an air-gapped plant network is normal.
#
# Like the Node build it embeds the interpreter, so it is platform-specific and
# cannot be cross-compiled; CI builds one per OS (.github/workflows/release.yml).
#
# Run from the repo root:
#   uv run --group packaging pyinstaller \
#       --distpath packages/server-python/dist \
#       --workpath packages/server-python/build-pyinstaller \
#       packages/server-python/packaging/opcua-mcp-server.spec

import platform
import sys
from pathlib import Path

HERE = Path(SPECPATH).resolve()  # noqa: F821 — injected by PyInstaller
PKG_ROOT = HERE.parent
REPO_ROOT = PKG_ROOT.parents[1]

# `contract.py` looks for `tools.json` beside the package before falling back to
# the repo root. Only the first of those exists inside a frozen app, so the
# canonical contract is staged there — the same placement the wheel uses, and for
# the same reason.
datas = [(str(REPO_ROOT / "contract" / "tools.json"), "opcua_mcp_server")]

# Match the Node build's artifact naming exactly, so a release page lists the two
# runtimes' executables side by side under one obvious scheme. `platform.machine()`
# spells the same architecture differently per OS, so normalise to Node's names.
ARCH = {
    "x86_64": "x64",
    "amd64": "x64",
    "AMD64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}[platform.machine()]

# `cli.main` imports the server lazily so that `--help` and `--install` do not
# probe the OPC UA endpoint. That import is inside a function, so it is named
# here rather than left to static analysis.
hiddenimports = ["opcua_mcp_server.server"]

a = Analysis(  # noqa: F821
    [str(HERE / "entry.py")],
    pathex=[str(PKG_ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Pulled in by the `mcp[cli]` extra for its own command line, which this
        # server never uses; excluding them keeps tens of MB out of the binary.
        "tkinter",
        "test",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"opcua-mcp-server-python-{sys.platform}-{ARCH}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off on purpose: compressed binaries trip antivirus heuristics, and
    # the people running this are on locked-down plant machines where a false
    # positive costs more than the megabytes save.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
