"""How much one call may ask for, resolved from ``contract/tools.json`` -> ``limits``.

``limits.ts`` is the Node half. Free of any OPC UA type so both unit suites can
drive the same table through it, which is how the browse caps and the
subscription defaults are already kept in step.

The caps are refusals rather than tuning knobs, and the one with any logic to it
is the history cap: ``num_values: 0`` used to mean "every reading in the range",
which against a node historised at 100ms is a request that never returns. It now
means "as many as allowed".
"""

from __future__ import annotations

import json
import math
from typing import Any, TypeVar

from .contract import CONTRACT
from .errors import message

LIMITS = CONTRACT["limits"]
MAX_NODES_PER_READ: int = LIMITS["maxNodesPerRead"]
MAX_NODES_PER_WRITE: int = LIMITS["maxNodesPerWrite"]
MAX_METHOD_ARGUMENTS: int = LIMITS["maxMethodArguments"]
MAX_HISTORY_VALUES: int = LIMITS["maxHistoryValues"]
MAX_SUBSCRIPTIONS: int = LIMITS["maxSubscriptions"]
MAX_EVENT_BUFFER_SIZE: int = LIMITS["maxEventBufferSize"]
MAX_REQUEST_BYTES: int = LIMITS["maxRequestBytes"]
MAX_STRING_BYTES: int = LIMITS["maxStringBytes"]
MAX_BYTE_STRING_BYTES: int = LIMITS["maxByteStringBytes"]
MAX_ARRAY_ITEMS: int = LIMITS["maxArrayItems"]
MAX_NESTING_DEPTH: int = LIMITS["maxNestingDepth"]

T = TypeVar("T")


class LimitExceeded(ValueError):
    """A request refused for its size, worded from the contract.

    A ``ValueError`` so the call path that already turns a validation failure
    into a tool error does the same with this; its own type so a layer that
    wraps other failures — a write that reports "Failed to write nodes: …" —
    can tell a refusal this server made from a failure the plant reported, and
    leave the refusal's wording alone. ``ContractRefusal`` plays that part in the
    Node half.
    """


def history_values(num_values: float | int | None) -> int:
    """How many raw readings to ask the OPC UA server for.

    ``0``, absent, or anything past the cap resolves to the cap. A negative
    number is not a smaller request than zero — it is not a request at all — so
    it resolves the same way.
    """
    if not num_values or num_values < 0:
        return MAX_HISTORY_VALUES
    return min(int(num_values), MAX_HISTORY_VALUES)


def event_buffer_size(requested: float | int | None) -> int:
    """The event buffer ``subscribe_events`` will actually keep.

    A clamp rather than a refusal, like the data-change buffer beside it: the
    tool already reports the size it applied, so a caller that asked for more is
    told what it got. 0 or absent is the default, as it always was.
    """
    if not requested or requested < 0:
        return CONTRACT["events"]["defaults"]["bufferSize"]
    return min(int(requested), MAX_EVENT_BUFFER_SIZE)


def utf8_bytes(text: str) -> int:
    """The UTF-8 length of a string, which is what a limit on one is a limit on.

    Bytes rather than characters because bytes are what get encoded and sent: a
    string of 'é' is twice as large on the wire as its length says.
    ``surrogatepass`` counts a lone surrogate as three, which is what Node's
    ``Buffer.byteLength`` counts too — and without it, one such character in a
    request would raise here instead of being measured.
    """
    return len(text.encode("utf-8", "surrogatepass"))


def check_request_bounds(tool: str, arguments: Any) -> None:
    """Refuse a call whose arguments are larger than any tool needs (issue #139).

    Every tool, before its arguments are validated, authorized or converted to
    anything OPC UA: a request that fails here never reaches the policy layer's
    per-value checks, never allocates a Variant and never opens a service call.
    Walked before the validator because the validator's work grows with the
    request, and this walk stops at the first thing out of bounds — an array of
    a million items is refused on its length, not after its millionth element.

    The walk is also what makes the size check safe to run: the nesting bound is
    enforced while descending, so recursion here is at most
    ``MAX_NESTING_DEPTH`` deep, and ``json.dumps`` below it only ever sees a
    shallow document.

    ``limits.ts`` walks in the same order and words each refusal from the same
    template, and ``tests/fixtures/request-limits.json`` holds the two to it.
    """
    _walk(tool, arguments, "", 0)
    size = utf8_bytes(json.dumps(arguments, separators=(",", ":"), ensure_ascii=False))
    if size > MAX_REQUEST_BYTES:
        raise LimitExceeded(
            message("requestTooLarge", tool=tool, size=size, limit=MAX_REQUEST_BYTES)
        )


def _walk(tool: str, value: Any, path: str, depth: int) -> None:
    if isinstance(value, str):
        size = utf8_bytes(value)
        if size > MAX_STRING_BYTES:
            raise LimitExceeded(
                message(
                    "stringTooLong",
                    tool=tool,
                    argument=path or "arguments",
                    size=size,
                    limit=MAX_STRING_BYTES,
                )
            )
        return
    if not isinstance(value, (list, dict)):
        return

    # The arguments object itself is level 1, so the count is one a caller can
    # work out from what they sent without knowing how this walk is written.
    if depth + 1 > MAX_NESTING_DEPTH:
        raise LimitExceeded(
            message(
                "nestedTooDeep", tool=tool, argument=path or "arguments", limit=MAX_NESTING_DEPTH
            )
        )
    if isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            raise LimitExceeded(
                message(
                    "arrayTooLong",
                    tool=tool,
                    argument=path or "arguments",
                    count=len(value),
                    limit=MAX_ARRAY_ITEMS,
                )
            )
        for index, item in enumerate(value):
            _walk(tool, item, f"{path}[{index}]", depth + 1)
        return
    for key, item in value.items():
        _walk(tool, item, f"{path}.{key}" if path else str(key), depth + 1)


def effective_limit(project: int, server: float | int | None) -> int:
    """The bound that applies to one kind of service call: ours, or the server's.

    A server's OperationLimits can only lower a project limit, never raise it —
    the project limit is what this server has decided one call may cost,
    whatever the other end would tolerate. 0 is the spec's "no limit", and an
    unreadable node is the same answer: the project limit stands.
    """
    if (
        isinstance(server, bool)
        or not isinstance(server, (int, float))
        or not math.isfinite(server)
        or server <= 0
    ):
        return project
    return min(project, int(server))


def chunked(items: list[T], size: int) -> list[list[T]]:
    """``items`` in consecutive runs of at most ``size``, in order."""
    step = max(1, int(size))
    return [items[start : start + step] for start in range(0, len(items), step)]


def aggregate_intervals(start_ms: float, end_ms: float, interval_ms: float) -> int:
    """How many intervals an aggregate read asks the OPC UA server to compute.

    ``processing_interval`` 0 is the whole range as one interval. The count is
    what the server would have to produce, and what the reply would carry, so it
    is bounded the same way a raw read is — by ``limits.maxHistoryValues``.
    """
    if not interval_ms > 0:
        return 1
    return max(1, math.ceil(abs(end_ms - start_ms) / interval_ms))


__all__ = [
    "MAX_ARRAY_ITEMS",
    "MAX_BYTE_STRING_BYTES",
    "MAX_EVENT_BUFFER_SIZE",
    "MAX_HISTORY_VALUES",
    "MAX_METHOD_ARGUMENTS",
    "MAX_NESTING_DEPTH",
    "MAX_NODES_PER_READ",
    "MAX_NODES_PER_WRITE",
    "MAX_REQUEST_BYTES",
    "MAX_STRING_BYTES",
    "MAX_SUBSCRIPTIONS",
    "LimitExceeded",
    "aggregate_intervals",
    "check_request_bounds",
    "chunked",
    "effective_limit",
    "event_buffer_size",
    "history_values",
    "utf8_bytes",
]
