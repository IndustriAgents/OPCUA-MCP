"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: OPC UA endpoint both the server and the capability probes connect to.
SERVER_URL = os.getenv("OPCUA_SERVER_URL", "opc.tcp://localhost:4840")


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
    #: Retries after the first attempt. 0 disables retrying; -1 retries forever.
    max_retry: float = 3
    #: Session timeout asked of the OPC UA server, in ms. python-opcua derives its
    #: keep-alive period from this, so it is also how quickly a dead server is
    #: noticed while nothing is being read.
    session_timeout_ms: float = 60000


RECONNECT_DEFAULTS = ReconnectConfig()

#: An unlimited ``max_retry`` has no sum to wait through, so the budget is capped
#: at this many times ``max_delay_ms``. Waiting forever inside one tool call is
#: never the right answer.
_UNLIMITED_BUDGET_FACTOR = 4


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
        # -1 is "forever", which is why the floor here is -1 rather than 0.
        max_retry=_parse_number(
            env.get("OPCUA_RECONNECT_MAX_RETRY"),
            "OPCUA_RECONNECT_MAX_RETRY",
            RECONNECT_DEFAULTS.max_retry,
            -1,
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
