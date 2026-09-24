"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

#: OPC UA endpoint both the server and the capability probes connect to. Blank
#: counts as unset, as it does for every other setting and for the Node runtime's
#: `||`: an MCP client passes an unset optional field as an empty string, and ""
#: is not an endpoint anyone meant.
SERVER_URL = os.getenv("OPCUA_SERVER_URL") or "opc.tcp://localhost:4840"


@dataclass(frozen=True)
class ReconnectConfig:
    """How this server retries a connection that is refused or has dropped.

    The same four knobs, spelled the same way, exist in the Node server
    (``config.ts``): an operator who has tuned one deployment must not have to
    learn a second vocabulary to tune the other.
    """

    #: Delay before the first retry, in ms. Doubles each attempt up to ``max_delay_ms``.
    initial_delay_ms: float = 1000
    #: Ceiling for the doubling, in ms.
    max_delay_ms: float = 8000
    #: Retries after the first attempt, per connection round. 0 disables
    #: retrying; -1 never gives up across rounds but still bounds each one — see
    #: :func:`reconnect_delays`. A whole number from -1 to ``MAX_RETRY_LIMIT``.
    max_retry: int = 3
    #: Session timeout asked of the OPC UA server, in ms. python-opcua derives its
    #: keep-alive period from this, so it is also how quickly a dead server is
    #: noticed while nothing is being read.
    session_timeout_ms: float = 60000


RECONNECT_DEFAULTS = ReconnectConfig()

#: An unlimited ``max_retry`` has no sum to wait through, so the budget is capped
#: at this many times ``max_delay_ms``, and one round makes this many retries.
#: Waiting forever inside one tool call — or inside the startup warm-up — is
#: never the right answer. The Node server's ``UNLIMITED_ROUND_RETRIES``.
_UNLIMITED_BUDGET_FACTOR = 4

#: The largest ``OPCUA_RECONNECT_MAX_RETRY`` accepted. Not a tuning knob: a
#: refusal of a value that can only be a typo. A tool call waits out one whole
#: round, so a thousand retries at the default ceiling is already over two hours
#: inside one request — and ``1e9`` used to be accepted and handed to a loop
#: building a billion-entry list. The Node server refuses above the same number.
MAX_RETRY_LIMIT = 1000

#: How long a request that reports on the connection — ``tools/list`` and
#: ``get_server_status`` — waits for the startup warm-up, in ms from its start.
#:
#: The MCP transport no longer waits for the warm-up at all (#136), so requests
#: can arrive while it is still connecting. Against a plant that is up it takes
#: well under this, and waiting for it is what keeps the first catalogue from
#: being the core tools only and the first status from reading "not connected".
#: Against one that is down it can take the whole round, and past this point
#: those two requests answer from what is known rather than wait on it. The Node
#: server's ``WARM_UP_WAIT_MS`` is the same number.
WARM_UP_WAIT_MS = 3000

_RETRY_COUNT = re.compile(r"[+-]?[0-9]+")


def _parse_number(raw: str | None, name: str, fallback: float, minimum: float) -> float:
    """One numeric setting, worded exactly as the Node server words it.

    Both servers reject the same value with the same message, so a typo in a
    shared deployment configuration reads the same whichever runtime finds it.
    """
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f'{name} must be a number >= {minimum:g}, got "{raw}"') from None
    if value != value or value in (float("inf"), float("-inf")) or value < minimum:
        raise ValueError(f'{name} must be a number >= {minimum:g}, got "{raw}"')
    return value


def _parse_retry_count(raw: str | None, fallback: int) -> int:
    """``OPCUA_RECONNECT_MAX_RETRY``: a whole number from -1 to ``MAX_RETRY_LIMIT``.

    Stricter than the delays, because a count has no sensible fraction. ``2.5``
    used to be truncated to 2 here while Node handed it to node-opcua as it was,
    so one setting meant two different things; ``-0.5`` passed the ``>= -1``
    floor and meant nothing at all. ASCII digits only, so ``0x10`` and ``1e2`` —
    which JavaScript's ``Number`` reads and ``int`` does not — are refused on
    both runtimes rather than on one.
    """
    if raw is None or not raw.strip():
        return fallback
    text = raw.strip()
    value = int(text) if _RETRY_COUNT.fullmatch(text) else None
    if value is None or not -1 <= value <= MAX_RETRY_LIMIT:
        raise ValueError(
            f"OPCUA_RECONNECT_MAX_RETRY must be a whole number from -1 to "
            f'{MAX_RETRY_LIMIT}, got "{raw}"'
        )
    return value


def parse_reconnect_config(env: Mapping[str, str]) -> ReconnectConfig:
    """Parse the reconnection settings. Mirrors the Node ``parseReconnectConfig``."""
    return ReconnectConfig(
        initial_delay_ms=_parse_number(
            env.get("OPCUA_RECONNECT_INITIAL_DELAY_MS"),
            "OPCUA_RECONNECT_INITIAL_DELAY_MS",
            RECONNECT_DEFAULTS.initial_delay_ms,
            0,
        ),
        max_delay_ms=_parse_number(
            env.get("OPCUA_RECONNECT_MAX_DELAY_MS"),
            "OPCUA_RECONNECT_MAX_DELAY_MS",
            RECONNECT_DEFAULTS.max_delay_ms,
            0,
        ),
        max_retry=_parse_retry_count(
            env.get("OPCUA_RECONNECT_MAX_RETRY"), RECONNECT_DEFAULTS.max_retry
        ),
        session_timeout_ms=_parse_number(
            env.get("OPCUA_SESSION_TIMEOUT_MS"),
            "OPCUA_SESSION_TIMEOUT_MS",
            RECONNECT_DEFAULTS.session_timeout_ms,
            1000,
        ),
    )


def reconnect_delays(config: ReconnectConfig) -> list[float]:
    """The delays, in ms, between one connection attempt and the next.

    node-opcua does this with a backoff library; python-opcua has no retry of its
    own at all, so the sequence is spelled out here — and spelled out the same
    way, because an operator comparing the two runtimes' logs should see the same
    waits from the same settings. An unlimited ``max_retry`` yields the capped
    budget's worth of delays rather than an endless list.
    """
    retries = config.max_retry
    if retries < 0:
        retries = _UNLIMITED_BUDGET_FACTOR
    delays = []
    for attempt in range(int(retries)):
        delays.append(min(config.initial_delay_ms * 2**attempt, config.max_delay_ms))
    return delays


def reconnect_budget_ms(config: ReconnectConfig) -> float:
    """How long a full round of retries may take, in ms.

    The Node server computes the same number for the same settings, and uses it
    for the same purpose: the window a tool call will spend waiting for a
    connection to come back before giving up on it.
    """
    if config.max_retry < 0:
        return config.max_delay_ms * _UNLIMITED_BUDGET_FACTOR
    return max(sum(reconnect_delays(config)), config.initial_delay_ms)


_cached: ReconnectConfig | None = None


def reconnect_config() -> ReconnectConfig:
    """The process-wide reconnection settings, parsed once."""
    global _cached
    if _cached is None:
        _cached = parse_reconnect_config(os.environ)
    return _cached


def describe_reconnect(config: ReconnectConfig) -> str:
    """One-line, secret-free summary for the startup log."""
    retry = "unlimited" if config.max_retry < 0 else f"{config.max_retry:g}"
    return (
        f"retries={retry} "
        f"backoff={config.initial_delay_ms:g}..{config.max_delay_ms:g}ms "
        f"session-timeout={config.session_timeout_ms:g}ms"
    )
