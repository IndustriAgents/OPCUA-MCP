"""A pytest plugin that makes ``import opcua`` load ``asyncua`` instead.

Not an adapter: a measuring device. The Python runtime's pure translation code
(records.py, variant_codec.py, method_arguments.py, the status-code tables)
imports only ``opcua.ua``. Running the unit tests that pin that code, and the
shared fixture tables both runtimes are held to, with asyncua's ``ua`` module in
its place shows which of those translations survive the swap unchanged and
which depend on a python-opcua type. Tests of the synchronous tool bodies are
expected to fail under it (asyncua's calls are coroutines) and are not counted.

    cd tests && PYTHONPATH=../docs/asyncua-spike uv run --no-sync --with asyncua==2.0.1 \
        pytest -p opcua_alias -q unit/test_records.py ...
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name == "opcua" or name.startswith("opcua."):
            return importlib.util.spec_from_loader(name, self)
        return None

    def create_module(self, spec):
        return importlib.import_module("asyncua" + spec.name[len("opcua") :])

    def exec_module(self, module):
        pass


assert "opcua" not in sys.modules, "opcua was imported before the alias was installed"
sys.meta_path.insert(0, _Alias())
