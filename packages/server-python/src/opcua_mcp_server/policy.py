"""Deployment policy for the MCP tool catalog and control operations.

Tool visibility is a usability feature; :meth:`ToolPolicy.authorize` is the
security boundary and is called for every invocation before OPC UA is touched.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .contract import CONTRACT
from .errors import message
from .node_ids import namespace_uri_form, resolve_node_id

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
class ValueBound:
    """What an allowlisted node may be written, beyond being the right node.

    Node identity was the whole of write authorization, and it is the weakest
    link in the safety story: a model that correctly identified the right
    setpoint and hallucinated ``9999`` instead of ``99.9`` was fully authorized.
    The variant codec range-checks integers and refuses a lossy Int64, but that
    is *type* safety — ``9999`` is a perfectly good Double.

    ``minimum``, ``maximum`` and ``allowed`` are checked by
    :meth:`ToolPolicy.authorize`, before the OPC UA network is touched at all.
    ``max_change`` cannot be: it is a bound on the *move*, so it needs the node's
    current value, and it is enforced in the write path where that read already
    happens.
    """

    minimum: float | None = None
    maximum: float | None = None
    #: The only values this node accepts. Numbers, strings or booleans — a
    #: discrete node is usually the latter two.
    allowed: tuple[Any, ...] | None = None
    #: The largest absolute difference from the node's current value one write
    #: may make.
    max_change: float | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.minimum is None
            and self.maximum is None
            and self.allowed is None
            and self.max_change is None
        )


_BOUND_KEYS = {"node", "min", "max", "enum", "max_change"}


def _value_bound(entry: Any) -> tuple[str, ValueBound]:
    """One ``writable_nodes`` entry as (node id, bound).

    A bare string stays legal and carries no bound, so every policy file written
    before this existed keeps working and means exactly what it meant.
    """
    if isinstance(entry, str):
        return entry, ValueBound()
    if not isinstance(entry, Mapping):
        raise ValueError(
            f"writable_nodes entry must be a node id or an object, got {type(entry).__name__}"
        )
    unknown = set(entry) - _BOUND_KEYS
    if unknown:
        # Loud, because the failure it prevents is silent: an operator who writes
        # "minimum" instead of "min" believes a bound is in force and none is.
        raise ValueError(
            f"Unknown key in a writable_nodes entry: {sorted(unknown)[0]}. "
            f"Use one of: {', '.join(sorted(_BOUND_KEYS))}"
        )
    node = entry.get("node")
    if not isinstance(node, str) or not node.strip():
        raise ValueError("A writable_nodes entry must name a node")
    bound = ValueBound(
        minimum=_bound_number(entry, "min", node),
        maximum=_bound_number(entry, "max", node),
        allowed=_bound_enum(entry, node),
        max_change=_bound_number(entry, "max_change", node),
    )
    if bound.minimum is not None and bound.maximum is not None and bound.minimum > bound.maximum:
        raise ValueError(f'writable_nodes entry "{node}" has min above max')
    if bound.max_change is not None and bound.max_change < 0:
        raise ValueError(f'writable_nodes entry "{node}" has a negative max_change')
    return node, bound


def _bound_number(entry: Mapping[str, Any], key: str, node: str) -> float | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'writable_nodes entry "{node}" has a non-numeric {key}')
    return float(value)


def _bound_enum(entry: Mapping[str, Any], node: str) -> tuple[Any, ...] | None:
    values = entry.get("enum")
    if values is None:
        return None
    if not isinstance(values, list) or not values:
        raise ValueError(f'writable_nodes entry "{node}" has an empty or non-list enum')
    return tuple(values)


@dataclass(frozen=True)
class PolicyConfig:
    profile: str
    allowed_tools: frozenset[str] | None
    writable_nodes: frozenset[str]
    #: Per-node value bounds, keyed by the allowlist entry exactly as written.
    #: Resolved against the live NamespaceArray at check time, the same way the
    #: identity allowlist is, so an ``nsu=`` entry follows a renumbered server.
    value_bounds: Mapping[str, ValueBound]
    callable_methods: frozenset[str]
    acknowledge_alarms: bool
    allow_insecure_control: bool
    secure_channel: bool
    #: Whether a write outside the range the OPC UA server itself published
    #: (``EURange``) is allowed through. Refused by default: a bound the
    #: equipment declares is worth more than one a human retyped, and it is the
    #: only value bound that exists on a deployment with no policy file at all.
    allow_out_of_range_writes: bool


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

    # The environment variable is a comma-separated list of node ids and can
    # carry no bounds; a bounded node needs the policy file. Setting it replaces
    # the file's list outright rather than merging, as every other override here
    # does — a half-overridden allowlist is the kind of thing nobody can reason
    # about at three in the morning.
    writable = _csv(_value(env, "OPCUA_ALLOWED_WRITE_NODES"))
    if writable is None:
        writable = control.get("writable_nodes", [])
    if not isinstance(writable, list):
        raise ValueError("OPCUA_POLICY_FILE control.writable_nodes must be a list")
    bounds: dict[str, ValueBound] = {}
    for entry in writable:
        node, bound = _value_bound(entry)
        bounds[node] = bound
    writable = list(bounds)

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
    allow_out_of_range = _boolean(
        _value(env, "OPCUA_ALLOW_OUT_OF_RANGE_WRITES"),
        "OPCUA_ALLOW_OUT_OF_RANGE_WRITES",
        bool(file.get("allow_out_of_range_writes", False)),
    )
    security_policy = _value(env, "OPCUA_SECURITY_POLICY") or "None"

    return PolicyConfig(
        profile=profile,
        allowed_tools=allowed_tools,
        writable_nodes=frozenset(writable),
        value_bounds=MappingProxyType(bounds),
        callable_methods=frozenset(method_env),
        acknowledge_alarms=acknowledge,
        allow_insecure_control=allow_insecure,
        secure_channel=security_policy.lower() != "none",
        allow_out_of_range_writes=allow_out_of_range,
    )


def values_at(arguments: Mapping[str, Any], path: str) -> list[str]:
    """Every value a guard path selects out of a call's arguments.

    Supports ``field`` and ``array[].field``. A path that selects nothing yields
    nothing, and the caller treats that as a denial rather than a pass: an
    argument the guard expected and did not find means the call does not look
    like what the contract declared.
    """
    head, _, rest = path.partition(".")
    if head.endswith("[]"):
        items = arguments.get(head[:-2])
        if not isinstance(items, list):
            return []
        found: list[str] = []
        for item in items:
            if isinstance(item, Mapping):
                found.extend(values_at(item, rest))
        return found
    if rest:
        nested = arguments.get(head)
        return values_at(nested, rest) if isinstance(nested, Mapping) else []
    value = arguments.get(head)
    return [value] if isinstance(value, str) else []


def pairs_at(arguments: Mapping[str, Any], spec: Mapping[str, str]) -> list[tuple[str, Any]]:
    """Every (node id, value) a ``valuePaths`` declaration selects, element-wise.

    Unlike :func:`values_at`, which flattens because it only has to *collect*
    ids, this has to keep each target with the value aimed at it: a batch write
    is a list of independent (where, what) pairs and checking them crosswise
    would authorise a value against the wrong node's bound.

    An element carrying no node id yields nothing. That is not a hole — the node
    id is ``required`` by the contract schema and the identity allowlist has
    already refused a write whose target cannot be located.
    """
    items = arguments.get(spec["array"])
    if not isinstance(items, list):
        return []
    found: list[tuple[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        node_id = item.get(spec["nodeIdField"])
        if isinstance(node_id, str):
            found.append((node_id, item.get(spec["valueField"])))
    return found


def as_number(value: Any) -> float | None:
    """``value`` as a number for comparison, or None if it is not one.

    A string is parsed, because the write path accepts one for a numeric node
    and parses it — ``"42.5"`` reaches the plant as 42.5, so a bound that did not
    look inside the string would be trivially bypassed by quoting the number.

    A boolean is not a number here even though Python says it is. ``True`` is not
    1 to an operator writing a bound, and letting it compare as one is the same
    coercion the typed-argument work removed from method calls.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def format_number(value: float) -> str:
    """A number as a refusal should read it: 100 rather than 100.0.

    ``format_number`` in ``policy.ts`` is the other half, and the two must agree
    character for character — the refusals they build are compared by
    ``tests/e2e/test_runtime_differential.py``.
    """
    if value == float("inf"):
        return "infinity"
    if value == float("-inf"):
        return "-infinity"
    if float(value).is_integer() and abs(value) < 1e16:
        return str(int(value))
    return repr(float(value))


def _same_json_value(value: Any, candidate: Any) -> bool:
    """Whether a written value is one of an ``enum`` entry.

    Booleans and numbers are kept apart deliberately (``True`` is not 1), and a
    numeric string matches a numeric entry, because the write path parses it and
    the plant sees the number.
    """
    if isinstance(value, bool) or isinstance(candidate, bool):
        return value is candidate
    if isinstance(candidate, (int, float)):
        number = as_number(value)
        return number is not None and number == float(candidate)
    return value == candidate


class ToolPolicy:
    def __init__(self, config: PolicyConfig):
        self.config = config
        self._tools = {tool["name"]: tool for tool in CONTRACT["tools"]}
        #: The server's NamespaceArray, once a session has reported it. ``None``
        #: means "not yet known", which is not the same as "empty": an ``nsu=``
        #: allowlist entry cannot be resolved before the server has said what
        #: its namespaces are, and resolving it wrongly would authorise a write
        #: to whatever node happens to sit at that index. Unknown denies.
        self._namespaces: list[str] | None = None

    def bind_namespaces(self, uris: Sequence[str]) -> None:
        """Bind the live NamespaceArray, re-read on every (re)connect.

        Every connect, not just the first: a server that restarted may have
        loaded its namespaces in a different order, and an allowlist pinned by
        URI has to follow it there. That is the whole reason the ``nsu=`` form
        exists.
        """
        self._namespaces = list(uris)
        for entry in self.config.writable_nodes:
            if namespace_uri_form(entry) and resolve_node_id(entry, self._namespaces) is None:
                # Loud, because the failure it prevents is silent: the operator
                # believes a node is writable and every attempt is denied.
                print(
                    f'WARNING: policy entry "{entry}" names a namespace URI this server does '
                    f"not publish, so nothing can match it. Check the NamespaceArray with "
                    f"get_server_status.",
                    file=sys.stderr,
                )

    def _class_visible(self, tool: dict[str, Any]) -> bool:
        """Whether a tool is offered at all, from its access class and its guard.

        Exhaustive over :data:`ACCESS_CLASSES` and closed by default. The
        previous version ended in ``return bool(self.config.writable_nodes)``, so
        an access class it did not recognise — a typo, or one added to the
        contract later — fell into the *write* branch and became visible.
        Anything unrecognised is now denied, and ``full`` does not exempt it: a
        profile meaning "no allowlists" must not also mean "no idea what this
        is, so yes".
        """
        access_class = tool["accessClass"]
        if access_class not in ACCESS_CLASSES:
            return False
        if access_class == "read":
            return True
        if access_class == "monitor":
            # Deliberately not behind the secure-channel gate, and this is the
            # place to say why. That gate exists to stop *control* over a channel
            # anyone can read or forge. A subscription costs the server resources
            # and delivers values, but changes nothing in the plant — it is read,
            # arriving by a different route. Gating it would deny the default
            # profile (``observe``) its main tool on exactly the deployments that
            # need to watch something before they are allowed to touch it.
            return True

        # A control tool must declare what needs authorising. Without that the
        # policy has nothing to check, and "nothing to check" must never read as
        # "nothing to stop it".
        guard = tool.get("guard")
        if not guard:
            return False

        if not self.config.secure_channel and not self.config.allow_insecure_control:
            return False
        if self.config.profile == "full":
            return True
        if self.config.profile != "operator":
            return False

        # Under ``operator``, a tool is offered only if its allowlist could ever
        # say yes. Derived from the guard, so a new control tool needs no code
        # here.
        if guard.get("flag") == "acknowledgeAlarms":
            return self.config.acknowledge_alarms
        if guard.get("methodPaths"):
            return bool(self.config.callable_methods)
        if guard.get("nodeIdPaths"):
            return bool(self.config.writable_nodes)
        return False

    def is_visible(self, tool: dict[str, Any]) -> bool:
        allowed = self.config.allowed_tools
        return self._class_visible(tool) and (allowed is None or tool["name"] in allowed)

    def authorize(self, name: str, arguments: Mapping[str, Any] | None = None) -> None:
        """Authorize one call, or raise. The security boundary.

        Walks the tool's declared ``guard`` rather than switching on its name, so
        a control tool added to the contract is checked by this code without it
        changing — and is denied outright if it declares no guard.
        """
        arguments = arguments or {}
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(message("unknownTool", tool=name))
        if not self.is_visible(tool):
            raise PermissionError(message("toolDisabled", tool=name, profile=self.config.profile))
        if self.config.profile != "operator":
            return

        # ``is_visible`` has already refused a guardless control tool; this is
        # the same refusal stated where the arguments are checked, so neither
        # half can be removed on the assumption that the other covers it.
        guard = tool.get("guard")
        if not guard:
            if tool["accessClass"] in {"read", "monitor"}:
                return
            raise PermissionError(message("guardMissing", tool=name))

        for path in guard.get("nodeIdPaths", []):
            found = values_at(arguments, path)
            if not found:
                # The guard named an argument the call does not carry. Denying is
                # the only safe reading: a write whose target cannot be located
                # is a write whose target cannot be checked.
                raise PermissionError(
                    f"{name} requires {path.replace('[]', '')} to authorize the write"
                )
            for node_id in found:
                self._require_writable(node_id)

        # And what is being written, not only where. Node identity was the whole
        # of write authorization, and a model that correctly identified the right
        # setpoint and hallucinated 9999 instead of 99.9 was fully authorized.
        for spec in guard.get("valuePaths", []):
            for node_id, value in pairs_at(arguments, spec):
                self._require_value_allowed(node_id, value)

        for pair in guard.get("methodPaths", []):
            objects = values_at(arguments, pair["objectPath"])
            methods = values_at(arguments, pair["methodPath"])
            if not objects or not methods:
                raise PermissionError(
                    f"{name} requires {pair['objectPath']} and {pair['methodPath']} "
                    f"to authorize the call"
                )
            wanted_object = self._resolve(objects[0])
            wanted_method = self._resolve(methods[0])
            allowed = set()
            for entry in self.config.callable_methods:
                entry_object, _, entry_method = entry.partition("|")
                resolved_object = self._resolve(entry_object)
                resolved_method = self._resolve(entry_method)
                # An entry naming a namespace this server does not publish is
                # dropped rather than kept as some placeholder. Keeping it would
                # let two *different* unresolvable ids compare equal to each
                # other, which is how an unknown URI could authorise an unknown
                # request.
                if resolved_object is not None and resolved_method is not None:
                    allowed.add(f"{resolved_object}|{resolved_method}")
            if (
                wanted_object is None
                or wanted_method is None
                or f"{wanted_object}|{wanted_method}" not in allowed
            ):
                raise PermissionError(
                    message(
                        "methodNotAllowed",
                        object_node_id=objects[0],
                        method_node_id=methods[0],
                    )
                )

    def _resolve(self, node_id: str) -> str | None:
        """One node id in the spelling this policy compares by, or None if it has none.

        None means "names a namespace this server does not publish". Callers must
        treat that as a denial and must not substitute a placeholder: two
        *different* unresolvable ids sharing one placeholder would compare equal,
        so an allowlist entry for an unknown URI would authorise a request naming
        a different unknown URI. The first draft did exactly that, and a test
        caught it.
        """
        return resolve_node_id(node_id, self._namespaces or [])

    def _resolved_set(self, entries: Iterable[str]) -> set[str]:
        """Every allowlist entry that resolves, in comparable form.

        Entries that do not resolve are dropped rather than kept — an entry
        naming a namespace this server does not publish can match nothing, and
        that is the whole of what it should do.
        """
        resolved = (self._resolve(entry) for entry in entries)
        return {entry for entry in resolved if entry is not None}

    def _require_writable(self, node_id: str) -> None:
        wanted = self._resolve(node_id)
        if wanted is None or wanted not in self._resolved_set(self.config.writable_nodes):
            raise PermissionError(message("nodeNotWritable", node_id=node_id))

    def bound_for(self, node_id: str) -> ValueBound | None:
        """The operator's value bound for ``node_id``, or None if it has none.

        Resolved rather than compared literally, for the same reason
        :meth:`_require_writable` is: ``nsu=…;i=5`` and ``ns=2;i=5`` are the same
        node on a server that publishes that URI at index 2, and a bound that
        only matched one spelling would be a bound an operator believes is in
        force and is not.

        Public because the write path needs it too: ``max_change`` is a bound on
        the *move*, so it can only be checked against the node's current value,
        which is a read and does not belong in the authorization layer.
        """
        wanted = self._resolve(node_id)
        if wanted is None:
            return None
        for entry, bound in self.config.value_bounds.items():
            if not bound.is_empty and self._resolve(entry) == wanted:
                return bound
        return None

    def _require_value_allowed(self, node_id: str, value: Any) -> None:
        """Check one write against the operator's bound for its target.

        Everything here is decidable without touching the network, which is what
        keeps it in the authorization layer: a refusal happens before a single
        byte is sent, so a batch can never end up partially applied — the same
        property the identity allowlist already had.
        """
        bound = self.bound_for(node_id)
        if bound is None:
            return
        # An array write is checked element by element. Writing [0, 9999] to a
        # bounded node is writing 9999 to it.
        for element in value if isinstance(value, list) else [value]:
            self._check_one(node_id, element, bound)

    def _check_one(self, node_id: str, value: Any, bound: ValueBound) -> None:
        if bound.allowed is not None and not any(
            _same_json_value(value, candidate) for candidate in bound.allowed
        ):
            raise PermissionError(
                message(
                    "valueNotAllowed",
                    value=json.dumps(value),
                    node_id=node_id,
                    allowed=", ".join(json.dumps(item) for item in bound.allowed),
                )
            )
        if bound.minimum is None and bound.maximum is None:
            return
        number = as_number(value)
        if number is None:
            raise PermissionError(
                message("valueNotComparable", node_id=node_id, value=json.dumps(value))
            )
        low = bound.minimum if bound.minimum is not None else float("-inf")
        high = bound.maximum if bound.maximum is not None else float("inf")
        if not low <= number <= high:
            raise PermissionError(
                message(
                    "valueOutOfRange",
                    value=format_number(number),
                    node_id=node_id,
                    low=format_number(low),
                    high=format_number(high),
                    unit="",
                    source="the operator policy",
                )
            )


@lru_cache(maxsize=1)
def tool_policy() -> ToolPolicy:
    return ToolPolicy(parse_policy_config(os.environ))


def describe_policy(policy: ToolPolicy) -> str:
    """One-line summary for the startup log.

    The three states are named apart. The old version printed
    ``insecure-control=enabled`` both for a properly secured deployment and for
    an active lab override, which made the override the opposite of conspicuous
    — the one line an operator might scan for it said the same thing either way.
    """
    config = policy.config
    if config.secure_channel:
        control = "secured"
    elif config.allow_insecure_control:
        control = "INSECURE-OVERRIDE"
    else:
        control = "blocked"
    return f"profile={config.profile} control={control}"
