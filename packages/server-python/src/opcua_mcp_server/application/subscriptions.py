"""Subscription lifecycle decisions over an instance-owned, client-lazy port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..completeness import buffer_completeness
from ..contract import CONTRACT
from ..errors import AdapterFailure, ApplicationRefusal, describe_error, message
from ..limits import MAX_SUBSCRIPTIONS

DEFAULT_DATA_CHANGE_TRIGGER = CONTRACT["subscriptions"]["defaultDataChangeTrigger"]
DEADBAND_TYPES = dict.fromkeys(("none", "absolute", "percent"))
DATA_CHANGE_TRIGGERS = dict.fromkeys(("status", "statusValue", "statusValueTimestamp"))


@dataclass(frozen=True)
class Filter:
    """What a subscription reports, beyond how often it looks.

    Point a subscription at a noisy analogue tag with no deadband and the default
    20-record ring fills with sensor jitter in about a second: the agent reads it
    back, sees nothing but noise, and has spent one of the server's subscriptions
    to get it. This is OPC UA's own answer (Part 4 §7.22) rather than filtering
    after the fact — the values never leave the server, so it costs no bandwidth
    and no buffer.
    """

    deadband_type: str = "none"
    deadband_value: float = 0.0
    trigger: str = DEFAULT_DATA_CHANGE_TRIGGER

    @property
    def is_default(self) -> bool:
        """True when this asks for nothing the server would not do anyway."""
        return self.deadband_type == "none" and self.trigger == DEFAULT_DATA_CHANGE_TRIGGER


def resolve_filter(
    deadband_type: str | None,
    deadband_value: float | None,
    data_change_trigger: str | None,
) -> Filter:
    """The filter a subscribe request resolves to, or raise if it cannot.

    Validation the contract's own schema cannot express: the `enum` keyword
    refuses an unknown name, but "a deadband needs a size" is a relationship
    *between* two arguments. Refused rather than defaulted to zero, which would
    be a deadband that filters nothing while reporting that one is in force —
    the caller would read a buffer full of jitter and conclude the tag was
    noisier than their threshold, which it may not be.
    """
    kind = deadband_type or "none"
    if kind not in DEADBAND_TYPES:
        raise ValueError(
            message(
                "notAllowedValue",
                tool="subscribe_opcua_nodes",
                argument="deadband_type",
                allowed=", ".join(f'"{name}"' for name in DEADBAND_TYPES),
                value=f'"{kind}"',
            )
        )
    trigger = data_change_trigger or DEFAULT_DATA_CHANGE_TRIGGER
    if trigger not in DATA_CHANGE_TRIGGERS:
        raise ValueError(
            message(
                "notAllowedValue",
                tool="subscribe_opcua_nodes",
                argument="data_change_trigger",
                allowed=", ".join(f'"{name}"' for name in DATA_CHANGE_TRIGGERS),
                value=f'"{trigger}"',
            )
        )
    if kind == "none":
        return Filter(deadband_type="none", deadband_value=0.0, trigger=trigger)
    if deadband_value is None:
        raise ValueError(message("deadbandNeedsValue", deadband_type=kind))
    return Filter(
        deadband_type=kind,
        deadband_value=_number(deadband_value, 0.0),
        trigger=trigger,
    )


def _number(value: Any, fallback: float) -> float:
    """``value`` as a float, or ``fallback`` when it is absent or not a number."""
    if value is None or isinstance(value, bool):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return fallback if number != number else number  # NaN is never equal to itself


class SubscriptionPort(Protocol):
    def list(self) -> list[dict]: ...
    async def ranges(self, node_ids: list[str]) -> dict[str, bool]: ...
    async def subscribe(self, node_id: str, options: dict, data_filter: Filter) -> dict: ...
    async def unsubscribe(self, subscription_id: str) -> dict: ...


def list_subscriptions(port: SubscriptionPort) -> dict:
    records = port.list()
    return {"records": records, "completeness": buffer_completeness(records)}


async def subscribe_nodes(
    port: SubscriptionPort, node_ids: list[str], options: dict, data_filter: Filter
) -> dict:
    if not node_ids:
        raise ApplicationRefusal(
            message("emptyArray", tool="subscribe_opcua_nodes", argument="node_ids")
        )
    active = len(port.list())
    if active + len(node_ids) > MAX_SUBSCRIPTIONS:
        raise ApplicationRefusal(
            message(
                "tooManySubscriptions", active=active, limit=MAX_SUBSCRIPTIONS, wanted=len(node_ids)
            )
        )
    if data_filter.deadband_type == "percent":
        ranges = await port.ranges(node_ids)
        for node_id in node_ids:
            if not ranges.get(node_id):
                raise ApplicationRefusal(message("percentDeadbandNeedsRange", node_id=node_id))
    records = []
    for node_id in node_ids:
        try:
            records.append(await port.subscribe(node_id, options, data_filter))
        except Exception as error:
            raise AdapterFailure(
                "subscribe",
                message("subscribeFailed", node_id=node_id, reason=describe_error(error)),
                error,
            ) from error
    return {"records": records, "completeness": buffer_completeness(records)}


async def unsubscribe_nodes(port: SubscriptionPort, ids: list[str]) -> dict:
    if not ids:
        raise ApplicationRefusal(
            message("emptyArray", tool="unsubscribe_opcua_nodes", argument="subscription_ids")
        )
    active = {record["subscription_id"] for record in port.list()}
    unknown = [entry for entry in ids if entry not in active]
    if unknown:
        raise ApplicationRefusal(
            message("unknownSubscription", subscription_id=unknown[0])
            if len(unknown) == 1
            else message("unknownSubscriptions", subscription_ids=", ".join(unknown))
        )
    records = []
    for subscription_id in ids:
        records.append(await port.unsubscribe(subscription_id))
    return {"records": records, "completeness": buffer_completeness(records)}
