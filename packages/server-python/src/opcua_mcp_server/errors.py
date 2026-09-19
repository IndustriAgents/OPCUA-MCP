"""The wording of every failure a tool call can return, from the contract.

``errors.ts`` is the Node half, and the two are the same twenty lines on purpose:
the messages themselves live in ``contract/tools.json`` -> ``errors``, and each
runtime only substitutes into them. They used to be hand-mirrored string literals
in ``tools.ts`` and ``server.py``, which held for the message *bodies* — someone
checked — and quietly failed for the framing: the Node server wrapped every
failure as ``Error: <message>`` and this one did not, so the same failure read
two ways to a model and no test compared them.

A tool failure is returned with ``isError`` set, which already says it is an
error, so neither runtime prefixes it now.
"""

from __future__ import annotations

from typing import Any

from .contract import CONTRACT

#: The templates, straight from the contract. Keys that start with ``$`` are
#: documentation for a human reading the file, never a message.
TEMPLATES: dict[str, str] = {
    key: value for key, value in CONTRACT["errors"].items() if not key.startswith("$")
}


def message(key: str, **fields: Any) -> str:
    """One contract error message with its placeholders filled in.

    An unknown key raises rather than returning something placeholder-shaped: a
    message this server cannot word is a bug in this server, and finding it at
    the first call beats shipping ``{reason}`` to an operator.
    """
    try:
        template = TEMPLATES[key]
    except KeyError:
        raise KeyError(f"No such contract error template: {key}") from None
    return template.format(**fields)


__all__ = ["TEMPLATES", "message"]
