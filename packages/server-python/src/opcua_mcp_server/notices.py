"""Messages a tool adds *beside* a successful result, from the contract.

``notices.ts`` is the Node half. Separate from :mod:`errors` because a notice is
not a failure: it rides along with a result that is otherwise complete, as a
trailing plain-text content block that is deliberately outside
``structuredContent`` — a notice is not a record, and a client reading the
structured result must not have to filter it out.
"""

from __future__ import annotations

from typing import Any

from .contract import CONTRACT

TEMPLATES: dict[str, str] = {
    key: value for key, value in CONTRACT["notices"].items() if not key.startswith("$")
}


def notice(key: str, **fields: Any) -> str:
    """One contract notice with its placeholders filled in."""
    try:
        template = TEMPLATES[key]
    except KeyError:
        raise KeyError(f"No such contract notice template: {key}") from None
    return template.format(**fields)


__all__ = ["TEMPLATES", "notice"]
