"""The SBOM step release.yml runs over every asset it attaches (#145).

The workflow itself only runs on a tag, so these pin down the parts of
`scripts/sbom.py` that decide what a release's SBOMs say: which recipe an asset
gets, and how a lockfile export is turned into a document about one file.
"""

from __future__ import annotations

import importlib.util

import pytest
from conftest import ROOT

_spec = importlib.util.spec_from_file_location("release_sbom", ROOT / "scripts" / "sbom.py")
sbom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sbom)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("opcua-mcp-server-0.5.1.mcpb", "mcpb"),
        ("opcua-mcp-server-0.5.1.tgz", "npm"),
        ("opcua_mcp_server-0.5.1-py3-none-any.whl", "wheel"),
        ("opcua_mcp_server-0.5.1.tar.gz", "sdist"),
        ("opcua-mcp-server-node-linux-x64", "node-sea"),
        ("opcua-mcp-server-node-win32-x64.exe", "node-sea"),
        ("opcua-mcp-server-python-darwin-arm64", "pyinstaller"),
        ("opcua-mcp-server-python-win32-x64.exe", "pyinstaller"),
        # Its own output is not an asset, so a re-run does not SBOM an SBOM.
        ("opcua-mcp-server-node-linux-x64.cdx.json", None),
    ],
)
def test_every_release_asset_has_a_recipe(name, kind):
    assert sbom.classify(name) == kind


def test_an_unrecognised_asset_is_refused():
    """A new artifact must not reach a release without an SBOM decision."""
    with pytest.raises(ValueError, match="no SBOM recipe"):
        sbom.classify("opcua-mcp-server-0.5.1.dmg")


def _base() -> dict:
    """A minimal lockfile export, shaped like `npm sbom` output — duplicates included."""
    dup = {"bom-ref": "b@1", "type": "library", "name": "b", "version": "1"}
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {
            "component": {"bom-ref": "root@0.5.1", "type": "library", "name": "server-node"}
        },
        "components": [
            {"bom-ref": "a@1", "type": "library", "name": "a", "version": "1"},
            dup,
            dict(dup),
        ],
        "dependencies": [
            {"ref": "root@0.5.1", "dependsOn": ["a@1"]},
            {"ref": "a@1", "dependsOn": ["b@1"]},
            {"ref": "b@1", "dependsOn": []},
            {"ref": "b@1", "dependsOn": []},
        ],
    }


RUNTIME = {"bom-ref": "node@22.14.0", "type": "platform", "name": "node", "version": "22.14.0"}


def _finish(base=None, **overrides):
    kwargs = {
        "asset_name": "opcua-mcp-server-node-linux-x64",
        "asset_digest": "ab" * 32,
        "kind": "node-sea",
        "version": "0.5.1",
        "embedded": [RUNTIME],
        "properties": [{"name": "opcua-mcp:lockfile", "value": "x sha256:00"}],
    }
    kwargs.update(overrides)
    return sbom.finish(base or _base(), **kwargs)


def test_the_asset_is_the_subject_identified_by_its_digest():
    doc = _finish()
    subject = doc["metadata"]["component"]
    assert subject["name"] == "opcua-mcp-server-node-linux-x64"
    assert subject["type"] == "application"
    assert subject["hashes"] == [{"alg": "SHA-256", "content": "ab" * 32}]


def test_the_package_and_embedded_runtime_are_the_assets_dependencies():
    doc = _finish()
    graph = {d["ref"]: d.get("dependsOn", []) for d in doc["dependencies"]}
    assert graph[doc["metadata"]["component"]["bom-ref"]] == ["root@0.5.1", "node@22.14.0"]
    # The package keeps its own subtree, under the published name.
    assert graph["root@0.5.1"] == ["a@1"]
    package = next(c for c in doc["components"] if c["bom-ref"] == "root@0.5.1")
    assert package["name"] == "opcua-mcp-server"
    assert RUNTIME in doc["components"]


def test_duplicate_components_from_the_export_are_collapsed():
    """`npm sbom` lists a package once per install location; the schema forbids it."""
    doc = _finish()
    refs = [c["bom-ref"] for c in doc["components"]]
    assert len(refs) == len(set(refs))
    assert [d["ref"] for d in doc["dependencies"]].count("b@1") == 1


def test_a_package_asset_is_a_library_with_no_embedded_runtime():
    doc = _finish(asset_name="opcua-mcp-server-0.5.1.tgz", kind="npm", embedded=[])
    assert doc["metadata"]["component"]["type"] == "library"
    graph = {d["ref"]: d.get("dependsOn", []) for d in doc["dependencies"]}
    assert graph["asset:opcua-mcp-server-0.5.1.tgz"] == ["root@0.5.1"]


def test_build_inputs_are_recorded_as_properties():
    doc = _finish()
    assert {"name": "opcua-mcp:lockfile", "value": "x sha256:00"} in doc["metadata"]["properties"]


def test_the_serial_number_is_stable_for_the_same_bytes():
    """A re-run over the same asset must not produce a different document id."""
    assert _finish()["serialNumber"] == _finish()["serialNumber"]
    assert _finish()["serialNumber"] != _finish(asset_digest="cd" * 32)["serialNumber"]


def test_the_base_export_is_not_modified():
    base = _base()
    _finish(base)
    assert base == _base()


def test_an_empty_export_is_refused():
    base = _base()
    base["components"] = []
    with pytest.raises(ValueError, match="not a populated CycloneDX"):
        _finish(base)


def test_a_dangling_dependency_is_refused():
    base = _base()
    base["dependencies"].append({"ref": "a@1", "dependsOn": ["ghost@9"]})
    with pytest.raises(ValueError, match="ghost@9"):
        _finish(base)
