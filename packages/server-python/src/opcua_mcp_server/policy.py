"""Deployment policy for the MCP tool catalog and control operations.

Tool visibility is a usability feature; :meth:`ToolPolicy.authorize` is the
security boundary and is called for every invocation before OPC UA is touched.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .contract import CONTRACT

PROFILES = ("observe", "operator", "full")
ACCESS_CLASSES = ("read", "monitor", "alarm-action", "control")


def _value(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name, "").strip()
    return raw or None


def _boolean(raw: str | None, name: str, fallback: bool) -> bool:
    if raw is None:
        return fallback
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f'{name} must be true or false, got "{raw}"')


def _csv(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _profile(raw: str | None) -> str:
    normalized = (raw or "observe").lower()
    if normalized in {"read-only", "readonly"}:
        return "observe"
    if normalized not in PROFILES:
        raise ValueError(
            f'Invalid OPCUA_PROFILE: "{raw}". Use one of: observe, read-only, operator, full'
        )
    return normalized


def _load_policy_file(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Cannot read OPCUA_POLICY_FILE {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"OPCUA_POLICY_FILE {path} must contain a JSON object")
    if data.get("version", 1) != 1:
        raise ValueError(f"Unsupported OPC UA policy version {data['version']}; expected 1")
    return data


@dataclass(frozen=True)
class PolicyConfig:
    profile: str
    allowed_tools: frozenset[str] | None
    writable_nodes: frozenset[str]
    callable_methods: frozenset[str]
    acknowledge_alarms: bool
    allow_insecure_control: bool
    secure_channel: bool


def parse_policy_config(env: Mapping[str, str]) -> PolicyConfig:
    """Parse policy, with environment variables overriding the optional JSON file."""
    file = _load_policy_file(_value(env, "OPCUA_POLICY_FILE"))
    control = file.get("control") or {}
    if not isinstance(control, dict):
        raise ValueError("OPCUA_POLICY_FILE control must be an object")

    profile = _profile(_value(env, "OPCUA_PROFILE") or file.get("profile"))
    allowed = _csv(_value(env, "OPCUA_ALLOWED_TOOLS"))
    if allowed is None:
        allowed = file.get("allowed_tools")
    known = {tool["name"] for tool in CONTRACT["tools"]}
    if allowed is not None:
        unknown = set(allowed) - known
        if unknown:
            raise ValueError(f"Unknown tool in allowed_tools: {sorted(unknown)[0]}")
        allowed_tools = frozenset(allowed)
    else:
        allowed_tools = None

    writable = _csv(_value(env, "OPCUA_ALLOWED_WRITE_NODES"))
    if writable is None:
        writable = control.get("writable_nodes", [])

    method_env = _csv(_value(env, "OPCUA_ALLOWED_METHODS"))
    if method_env is None:
        method_env = [
            f"{item['object_id']}|{item['method_id']}"
            for item in control.get("callable_methods", [])
        ]
    for method in method_env:
        if "|" not in method:
            raise ValueError(
                f'Invalid OPCUA_ALLOWED_METHODS entry "{method}"; use object_node_id|method_node_id'
            )

    acknowledge = _boolean(
        _value(env, "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS"),
        "OPCUA_ALLOW_ACKNOWLEDGE_ALARMS",
        bool(control.get("acknowledge_alarms", False)),
    )
    allow_insecure = _boolean(
        _value(env, "OPCUA_ALLOW_INSECURE_CONTROL"),
        "OPCUA_ALLOW_INSECURE_CONTROL",
        bool(file.get("allow_insecure_control", False)),
    )
    security_policy = _value(env, "OPCUA_SECURITY_POLICY") or "None"

    return PolicyConfig(
        profile=profile,
        allowed_tools=allowed_tools,
        writable_nodes=frozenset(writable),
        callable_methods=frozenset(method_env),
        acknowledge_alarms=acknowledge,
        allow_insecure_control=allow_insecure,
        secure_channel=security_policy.lower() != "none",
    )


class ToolPolicy:
    def __init__(self, config: PolicyConfig):
        self.config = config
        self._tools = {tool["name"]: tool for tool in CONTRACT["tools"]}

    def _class_visible(self, tool: dict[str, Any]) -> bool:
        access_class = tool["accessClass"]
        if access_class in {"read", "monitor"}:
            return True
        if not self.config.secure_channel and not self.config.allow_insecure_control:
            return False
        if self.config.profile == "full":
            return True
        if self.config.profile != "operator":
            return False
        if access_class == "alarm-action":
            return self.config.acknowledge_alarms
        if tool["name"] == "call_opcua_method":
            return bool(self.config.callable_methods)
        return bool(self.config.writable_nodes)

    def is_visible(self, tool: dict[str, Any]) -> bool:
        allowed = self.config.allowed_tools
        return self._class_visible(tool) and (allowed is None or tool["name"] in allowed)

    def authorize(self, name: str, arguments: Mapping[str, Any] | None = None) -> None:
        arguments = arguments or {}
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"Unknown tool: {name}")
        if not self.is_visible(tool):
            raise PermissionError(
                f'Tool "{name}" is disabled by OPCUA_PROFILE={self.config.profile}'
            )
        if self.config.profile != "operator":
            return
        if name == "write_opcua_node":
            self._require_writable(str(arguments.get("node_id", "")))
        elif name == "write_multiple_opcua_nodes":
            items = arguments.get("nodes_to_write")
            if not isinstance(items, list):
                raise PermissionError("write_multiple_opcua_nodes requires a nodes_to_write array")
            for item in items:
                if not isinstance(item, Mapping):
                    raise PermissionError("Every batch item must contain a node_id")
                self._require_writable(str(item.get("node_id", "")))
        elif name == "call_opcua_method":
            key = f"{arguments.get('object_node_id', '')}|{arguments.get('method_node_id', '')}"
            if key not in self.config.callable_methods:
                raise PermissionError(f"Method {key} is not allowed by the operator policy")

    def _require_writable(self, node_id: str) -> None:
        if node_id not in self.config.writable_nodes:
            raise PermissionError(f"Node {node_id} is not writable under the operator policy")


@lru_cache(maxsize=1)
def tool_policy() -> ToolPolicy:
    return ToolPolicy(parse_policy_config(os.environ))


def describe_policy(policy: ToolPolicy) -> str:
    config = policy.config
    insecure = "enabled" if config.secure_channel or config.allow_insecure_control else "blocked"
    return f"profile={config.profile} insecure-control={insecure}"
