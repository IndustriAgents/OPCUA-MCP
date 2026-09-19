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

from .contract import CONTRACT

LIMITS = CONTRACT["limits"]
MAX_NODES_PER_READ: int = LIMITS["maxNodesPerRead"]
MAX_HISTORY_VALUES: int = LIMITS["maxHistoryValues"]
MAX_SUBSCRIPTIONS: int = LIMITS["maxSubscriptions"]


def history_values(num_values: float | int | None) -> int:
    """How many raw readings to ask the OPC UA server for.

    ``0``, absent, or anything past the cap resolves to the cap. A negative
    number is not a smaller request than zero — it is not a request at all — so
    it resolves the same way.
    """
    if not num_values or num_values < 0:
        return MAX_HISTORY_VALUES
    return min(int(num_values), MAX_HISTORY_VALUES)


def history_was_clipped(returned: int, wanted: int) -> bool:
    """Whether a raw history read stopped at the per-call maximum.

    Only when the cap itself was reached: a caller who asked for 10 and got 10
    has what they asked for, and telling them the range may hold more would be
    noise on every small read.
    """
    return wanted >= MAX_HISTORY_VALUES and returned >= MAX_HISTORY_VALUES


__all__ = [
    "MAX_HISTORY_VALUES",
    "MAX_NODES_PER_READ",
    "MAX_SUBSCRIPTIONS",
    "history_values",
    "history_was_clipped",
]
