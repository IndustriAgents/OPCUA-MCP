"""Build hook that stages the shared contract files inside the wheel.

Two files, both from `/contract`: `tools.json` (the tool surface) and
`config.json` (the configuration schema, #133). Everything below applies to both.

The canonical contract lives at the repo root (`/contract/tools.json`), outside
this package. A static `force-include` pointing at `../../` works when building
from a checkout, but breaks when the wheel is built *from an sdist* — which is
what `uv build` and `pip install <sdist>` do — because an sdist cannot contain
files from outside its own root:

    FileNotFoundError: Forced include not found: .../contract/tools.json

So the sdist carries its own copy at `contract/tools.json` (see the sdist
force-include in pyproject.toml), and this hook injects whichever copy exists
into the wheel at build time. It adds to `build_data` rather than writing into
the source tree, so building never leaves artefacts behind.
"""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: The files staged, and where the wheel carries each; `contract.py` reads them there.
STAGED = {
    "tools.json": "opcua_mcp_server/tools.json",
    "config.json": "opcua_mcp_server/config.json",
}


class ContractBuildHook(BuildHookInterface):
    PLUGIN_NAME = "opcua-contract"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return

        root = Path(self.root)
        for name, wheel_path in STAGED.items():
            candidates = (
                root.parents[1] / "contract" / name,  # repo checkout
                root / "contract" / name,  # unpacked sdist
            )
            found = next((c for c in candidates if c.is_file()), None)
            if found is None:
                raise FileNotFoundError(
                    f"Shared contract file {name} not found; looked in "
                    + ", ".join(str(c) for c in candidates)
                )
            build_data["force_include"][str(found)] = wheel_path
