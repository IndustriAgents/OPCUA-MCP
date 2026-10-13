"""The AccessLevel bits the information-model checks read, with no SDK behind them.

Apart from ``node_facts.py``, which reads the attribute, so that the pure rules
(``write_plan.py``, ``policy_check.py``) and the use cases in ``application/``
can test a bit without importing a client library.
"""

from __future__ import annotations

#: The bit of AccessLevel / UserAccessLevel that allows writing the Value.
CURRENT_WRITE = 0x02
#: The bit that says the server keeps the Value's history (Part 3 §5.6.2).
HISTORY_READ = 0x04


def allows(level: int | None, bit: int) -> bool | None:
    """Whether an AccessLevel byte has ``bit`` set; None when the level is unknown."""
    return None if level is None else bool(level & bit)


__all__ = ["CURRENT_WRITE", "HISTORY_READ", "allows"]
