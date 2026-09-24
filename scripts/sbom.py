"""Write a CycloneDX SBOM beside every release asset in a directory (#145).

    uv run --no-sync python scripts/sbom.py <dir>

For each asset ``X`` in ``<dir>`` this writes ``X.cdx.json``, and refuses to run
on a file it does not recognise, so a new artifact cannot reach a release
without somebody deciding what its bill of materials is. release.yml runs it on
every build runner, over exactly the files it is about to upload.

Where the dependency list comes from, per format:

  * npm tarball, ``.mcpb`` bundle, Node single-file executable: ``npm sbom``
    over ``packages/server-node/package-lock.json``, production dependencies
    only. The bundle and the executable carry that same tree, inlined by
    esbuild; the executable also embeds the Node runtime that built it, which
    is added as a component of its own.
  * wheel, sdist, Python single-file executable: ``uv export`` over
    ``uv.lock`` for the ``opcua-mcp-server`` package alone, without dev or
    workspace-only groups. The executable also embeds the CPython interpreter
    and the PyInstaller bootloader, both added as components.

Both lockfile exports list every platform's dependencies (``pywin32`` appears
in the Linux SBOM), so a binary's SBOM is a superset of what it carries, never
a subset. The asset itself becomes the SBOM's subject, identified by its
SHA-256, with the package it ships as its one direct dependency — so an SBOM
names the exact bytes it describes, not just a version. The lockfile digest
and build-tool versions go in as properties: the provenance attestation pins
the commit, and these pin what the build resolved from it.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE_PKG_DIR = ROOT / "packages" / "server-node"
NODE_LOCKFILE = NODE_PKG_DIR / "package-lock.json"
UV_LOCKFILE = ROOT / "uv.lock"

# Both runtimes publish under this one name, and `npm sbom` otherwise reports
# the package by its directory (`server-node`).
PACKAGE_NAME = "opcua-mcp-server"
REPOSITORY = "https://github.com/IndustriAgents/OPCUA-MCP"
SUFFIX = ".cdx.json"

NODE_KINDS = {"npm", "mcpb", "node-sea"}


def classify(name: str) -> str | None:
    """The kind of release asset ``name`` is, or None for an SBOM this wrote.

    Raises ValueError for anything else: a new artifact needs a decision about
    where its dependency list comes from, not a silent pass.
    """
    if name.endswith(SUFFIX):
        return None
    if name.startswith("opcua-mcp-server-node-"):
        return "node-sea"
    if name.startswith("opcua-mcp-server-python-"):
        return "pyinstaller"
    for suffix, kind in ((".mcpb", "mcpb"), (".tgz", "npm"), (".whl", "wheel")):
        if name.endswith(suffix):
            return kind
    if name.endswith(".tar.gz"):
        return "sdist"
    raise ValueError(f"no SBOM recipe for release asset {name!r}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed ({proc.returncode}):\n{proc.stderr}")
    return proc.stdout


def _tool(name: str) -> str:
    # Resolved to a full path: on Windows `npm` is `npm.cmd`, which subprocess
    # cannot find by its bare name.
    resolved = shutil.which(name)
    if resolved is None:
        raise SystemExit(f"{name} is required to generate the SBOM and is not on PATH")
    return resolved


def node_base() -> dict:
    """The production dependency tree from the npm lockfile."""
    out = _run(
        [
            _tool("npm"),
            "sbom",
            "--sbom-format",
            "cyclonedx",
            "--omit",
            "dev",
            "--package-lock-only",
        ],
        cwd=NODE_PKG_DIR,
    )
    return json.loads(out)


def python_base() -> dict:
    """The server package's runtime dependency tree from uv.lock."""
    out = _run(
        [
            _tool("uv"),
            "export",
            "--frozen",
            "--package",
            PACKAGE_NAME,
            "--no-dev",
            "--format",
            "cyclonedx1.5",
            # Experimental in uv; the shape check in `finish` is what catches a
            # change, and the uv version is recorded in the output.
            "--preview-features",
            "sbom-export",
        ],
        cwd=ROOT,
    )
    return json.loads(out)


def embedded_components(kind: str) -> list[dict]:
    """What a single-file executable carries besides the package: its runtime."""
    if kind == "node-sea":
        version = _run([_tool("node"), "-p", "process.versions.node"], cwd=ROOT).strip()
        return [
            {
                "type": "platform",
                "bom-ref": f"node@{version}",
                "name": "node",
                "version": version,
                "description": "Node.js runtime embedded in the single-file executable",
                "purl": f"pkg:generic/node@{version}?download_url=https://nodejs.org/dist/v{version}/",
            }
        ]
    if kind == "pyinstaller":
        # This script runs in the same uv environment PyInstaller built from,
        # so these are the interpreter and bootloader that were frozen in.
        from importlib.metadata import version as dist_version

        python = platform.python_version()
        bootloader = dist_version("pyinstaller")
        return [
            {
                "type": "platform",
                "bom-ref": f"cpython@{python}",
                "name": "cpython",
                "version": python,
                "description": "Python interpreter embedded in the single-file executable",
                "purl": f"pkg:generic/cpython@{python}",
            },
            {
                "type": "application",
                "bom-ref": f"pyinstaller@{bootloader}",
                "name": "pyinstaller",
                "version": bootloader,
                "description": "PyInstaller bootloader embedded in the single-file executable",
                "purl": f"pkg:pypi/pyinstaller@{bootloader}",
            },
        ]
    return []


def build_properties(kind: str) -> list[dict]:
    """The inputs a rebuild would need: lockfile digest and build-tool versions."""
    props: dict[str, str] = {}
    if kind in NODE_KINDS:
        props["opcua-mcp:lockfile"] = (
            f"packages/server-node/package-lock.json sha256:{sha256(NODE_LOCKFILE)}"
        )
        props["opcua-mcp:build:node"] = _run([_tool("node"), "--version"], cwd=ROOT).strip()
        props["opcua-mcp:build:npm"] = _run([_tool("npm"), "--version"], cwd=ROOT).strip()
    else:
        props["opcua-mcp:lockfile"] = f"uv.lock sha256:{sha256(UV_LOCKFILE)}"
        props["opcua-mcp:build:uv"] = _run([_tool("uv"), "--version"], cwd=ROOT).strip()
        props["opcua-mcp:build:python"] = platform.python_version()
    props["opcua-mcp:build:os"] = f"{sys.platform}-{platform.machine()}"
    commit = os.environ.get("GITHUB_SHA")
    if commit:
        props["opcua-mcp:source:commit"] = commit
    return [{"name": k, "value": v} for k, v in props.items()]


def finish(
    base: dict,
    *,
    asset_name: str,
    asset_digest: str,
    kind: str,
    version: str,
    embedded: list[dict],
    properties: list[dict],
) -> dict:
    """Make ``base`` (a lockfile export) describe one release asset.

    The exported root — the ``opcua-mcp-server`` package — moves into the
    component list, and the asset takes its place as the subject, so the
    dependency graph reads asset -> package (+ embedded runtime) -> libraries.
    """
    doc = copy.deepcopy(base)
    if doc.get("bomFormat") != "CycloneDX" or not doc.get("components"):
        raise ValueError(f"{asset_name}: lockfile export is not a populated CycloneDX document")
    package = doc.get("metadata", {}).get("component")
    if not package or "bom-ref" not in package:
        raise ValueError(f"{asset_name}: lockfile export has no root component")
    package["name"] = PACKAGE_NAME

    asset_ref = f"asset:{asset_name}"
    subject = {
        "type": "application" if kind in {"mcpb", "node-sea", "pyinstaller"} else "library",
        "bom-ref": asset_ref,
        "name": asset_name,
        "version": version,
        "hashes": [{"alg": "SHA-256", "content": asset_digest}],
        "externalReferences": [{"type": "vcs", "url": REPOSITORY}],
    }

    doc["serialNumber"] = (
        f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{asset_name}#{asset_digest}')}"
    )
    doc["metadata"]["component"] = subject
    doc["metadata"]["properties"] = [*doc["metadata"].get("properties", []), *properties]
    doc["components"] = _unique_components([package, *embedded, *doc["components"]])
    doc["dependencies"] = _merged_dependencies(
        [
            {
                "ref": asset_ref,
                "dependsOn": [package["bom-ref"], *(e["bom-ref"] for e in embedded)],
            },
            *({"ref": e["bom-ref"]} for e in embedded),
            *doc.get("dependencies", []),
        ]
    )
    check(doc)
    return doc


def check(doc: dict) -> None:
    """The structural rules a consumer's tooling will enforce, checked here first.

    Not a full schema validation (that would need a dependency this script
    deliberately does without), but the two things a lockfile export has
    actually got wrong: duplicate bom-refs, and a graph naming a component that
    is not in the document.
    """
    refs = [c["bom-ref"] for c in doc["components"]]
    if len(refs) != len(set(refs)):
        raise ValueError("duplicate bom-ref in components")
    known = {*refs, doc["metadata"]["component"]["bom-ref"]}
    for dep in doc["dependencies"]:
        dangling = {dep["ref"], *dep.get("dependsOn", [])} - known
        if dangling:
            raise ValueError(f"dependency graph names unknown components: {sorted(dangling)}")


def _unique_components(components: list[dict]) -> list[dict]:
    """One component per bom-ref, first wins.

    `npm sbom` lists a package once per place it is installed, so a package
    nested under two parents appears twice with the same bom-ref — which the
    CycloneDX schema rejects (bom-refs must be unique).
    """
    seen: set[str] = set()
    unique = []
    for component in components:
        if component["bom-ref"] not in seen:
            seen.add(component["bom-ref"])
            unique.append(component)
    return unique


def _merged_dependencies(dependencies: list[dict]) -> list[dict]:
    """One entry per ref, with the union of what each duplicate depended on."""
    merged: dict[str, list[str]] = {}
    for dep in dependencies:
        targets = merged.setdefault(dep["ref"], [])
        targets.extend(t for t in dep.get("dependsOn", []) if t not in targets)
    return [
        {"ref": ref, "dependsOn": targets} if targets else {"ref": ref}
        for ref, targets in merged.items()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="directory of release assets")
    args = parser.parse_args(argv)

    # Dotfiles are never assets: `uv build --out-dir` drops a `.gitignore` in
    # its output, and upload-artifact leaves hidden files behind anyway.
    assets = sorted(
        p for p in args.directory.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    kinds = {p: classify(p.name) for p in assets}
    targets = {p: k for p, k in kinds.items() if k is not None}
    if not targets:
        raise SystemExit(f"no release assets in {args.directory}")

    version = json.loads((NODE_PKG_DIR / "package.json").read_text(encoding="utf-8"))["version"]
    bases: dict[str, dict] = {}
    for path, kind in targets.items():
        family = "node" if kind in NODE_KINDS else "python"
        if family not in bases:
            bases[family] = node_base() if family == "node" else python_base()
        doc = finish(
            bases[family],
            asset_name=path.name,
            asset_digest=sha256(path),
            kind=kind,
            version=version,
            embedded=embedded_components(kind),
            properties=build_properties(kind),
        )
        out = path.with_name(path.name + SUFFIX)
        out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"{out.name}: {len(doc['components'])} components ({kind})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
