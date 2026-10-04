"""Enforce the extracted boundaries while the remaining monolith moves in slices."""

from __future__ import annotations

import ast
import re

from conftest import ROOT

PYTHON = ROOT / "packages/server-python/src/opcua_mcp_server"
NODE = ROOT / "packages/server-node/src"


def test_application_modules_do_not_import_protocol_or_native_sdks():
    for file in (PYTHON / "application").glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            if isinstance(node, ast.ImportFrom) and node.level:
                assert node.module not in {
                    "connection",
                    "server",
                    "state",
                    "node_metadata",
                    "records",
                }, file
                assert not (node.module or "").startswith("adapters"), file
            assert not any(name.split(".")[0] in {"mcp", "opcua", "asyncua"} for name in names), (
                file
            )
    for file in (NODE / "application").glob("*.ts"):
        text = file.read_text(encoding="utf-8")
        assert not re.search(r'from\s+["\'](?:@modelcontextprotocol/|node-opcua)', text), file
        assert not re.search(
            r'from\s+["\'][^"\']*(?:adapters/|(?:connection|tools|node-metadata|records)\.js)', text
        ), file


def test_native_adapters_do_not_import_the_mcp_server():
    for file in (PYTHON / "adapters").glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("mcp"), file
                assert node.module != "server", file
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith("mcp") for alias in node.names), file
    for file in (NODE / "adapters").glob("*.ts"):
        text = file.read_text(encoding="utf-8")
        assert not re.search(r'from\s+["\'](?:@modelcontextprotocol/|[^"\']*tools\.js)', text), file


def test_new_feature_files_stay_reviewable():
    for directory, extension in [
        (PYTHON / "application", "py"),
        (PYTHON / "adapters", "py"),
        (NODE / "application", "ts"),
        (NODE / "adapters", "ts"),
    ]:
        for file in directory.glob("*." + extension):
            assert len(file.read_text(encoding="utf-8").splitlines()) <= 400, file
