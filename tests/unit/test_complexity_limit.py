"""The configured complexity limit must reject a growing composition function."""

from __future__ import annotations

import shutil
import subprocess

from conftest import ROOT


def test_the_complexity_gate_accepts_25_paths_and_refuses_26(tmp_path):
    ruff = shutil.which("ruff")
    assert ruff, "Ruff is a required development dependency"
    source = tmp_path / "example.py"
    for branches in (24, 25):
        conditions = "\n".join(
            f"    if value == {index}:\n        return {index}" for index in range(branches)
        )
        source.write_text(f"def example(value):\n{conditions}\n    return -1\n", encoding="utf-8")
        checked = subprocess.run(
            [
                ruff,
                "check",
                "--config",
                str(ROOT / "pyproject.toml"),
                "--select",
                "C901",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert checked.returncode == (0 if branches == 24 else 1), checked.stdout + checked.stderr
        if branches == 25:
            assert "C901" in checked.stdout
