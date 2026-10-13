"""Explicit maintained-client qualification and one-release rollback selection."""

from __future__ import annotations

import os
from collections.abc import Mapping


def parse_python_backend(env: Mapping[str, str]) -> str:
    raw = env.get("OPCUA_PYTHON_BACKEND", "")
    selected = raw.strip().lower() or "asyncua"
    if selected not in {"asyncua", "legacy"}:
        raise ValueError(f'Invalid OPCUA_PYTHON_BACKEND: "{raw}". Use asyncua or legacy')
    return selected


def python_backend() -> str:
    return parse_python_backend(os.environ)
