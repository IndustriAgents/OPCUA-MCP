"""Conversion between the ISO-8601 strings MCP delivers and datetimes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

#: The accepted grammar: an RFC 3339 date-time. The date and time are always
#: both there; seconds and a 1-9 digit fraction are optional; the separator is
#: ``T``, ``t`` or a space; the zone is ``Z``/``z`` or ``+HH:MM``/``-HH:MM``.
#:
#: Written out rather than delegated to ``datetime.fromisoformat``, whose grammar
#: is not the same on every supported Python — 3.10 refuses ``Z`` and most of
#: ISO 8601, 3.11 accepts week dates and ``20260423T174000`` — and not the same
#: as the Node runtime's ``Date`` parser, which reads ``04/23/2026`` and
#: ``April 23, 2026`` and rolls ``2026-02-30`` over to March (#157). ``dates.ts``
#: is the other half, and ``tests/fixtures/datetime-parsing.json`` holds both to
#: the same table. ``[0-9]``, not ``\d``, which in Python also matches every
#: Unicode digit.
#:
#: The zone group is optional in the pattern only so a zone-less value can be
#: told apart from a malformed one: it is refused either way, with its own reason.
_DATE_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt ]([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?"
    r"([Zz]|[+-][0-9]{2}:[0-9]{2})?"
)

_MS_PER_DAY = 86_400_000
#: What an OPC UA DateTime can hold (Part 6 §5.2.2.5): 1601-01-01 is its zero,
#: and neither client library represents a year past 9999.
_MIN_MS = -11_644_473_600_000  # 1601-01-01T00:00:00Z
_MAX_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _invalid(value: str) -> ValueError:
    return ValueError(f'Invalid date/time: "{value}". Use ISO 8601, e.g. 2026-04-23T17:40:00Z')


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = (year % 4 == 0 and year % 100 != 0) or year % 400 == 0
        return 29 if leap else 28
    return (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 in the proleptic Gregorian calendar.

    Plain integer arithmetic (Hinnant's algorithm), the same as ``dates.ts``, so
    both runtimes reach the same instant without either calendar library in the
    way — Python's ``datetime`` cannot even hold the year 0000 to refuse it.
    """
    year -= month <= 2
    era = year // 400
    year_of_era = year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an optional RFC 3339 string into an aware UTC datetime.

    MCP delivers these as strings, so they are converted here before being handed
    to the opcua client. Three refusals, worded identically by the Node server's
    ``toDate``:

    * anything outside the grammar above, or a date or time that does not exist;
    * a value with no zone. Read as UTC it silently shifts a history window by
      the plant's offset from UTC; read as local time it depends on the host the
      server happens to run on. Neither is what the caller meant often enough to
      guess, and the fix is one character;
    * an instant outside what an OPC UA DateTime can carry.

    A fraction is truncated to milliseconds, the precision the Node runtime's
    ``Date`` holds, so both send the plant the same instant.
    """
    if value is None:
        return None
    match = _DATE_TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise _invalid(value)
    year, month, day, hour, minute = (int(part) for part in match.groups()[:5])
    second = int(match.group(6) or "0")
    millisecond = int((match.group(7) or "").ljust(3, "0")[:3])
    zone = match.group(8)
    if zone is None:
        raise ValueError(
            f'Invalid date/time: "{value}" has no timezone, so the instant it names '
            "depends on where it is read. Add Z for UTC or an offset such as +02:00, "
            "e.g. 2026-04-23T17:40:00Z"
        )

    offset_minutes = 0
    if zone not in ("Z", "z"):
        offset_hour, offset_minute = int(zone[1:3]), int(zone[4:6])
        if offset_hour > 23 or offset_minute > 59:
            raise _invalid(value)
        offset_minutes = (offset_hour * 60 + offset_minute) * (-1 if zone[0] == "-" else 1)
    if (
        not 1 <= month <= 12
        or not 1 <= day <= _days_in_month(year, month)
        or hour > 23
        or minute > 59
        or second > 59
    ):
        raise _invalid(value)

    instant = (
        _days_from_civil(year, month, day) * _MS_PER_DAY
        + ((hour * 60 + minute - offset_minutes) * 60 + second) * 1000
        + millisecond
    )
    if not _MIN_MS <= instant <= _MAX_MS:
        raise ValueError(
            f'Invalid date/time: "{value}" is outside what an OPC UA DateTime can hold '
            "(1601-01-01T00:00:00Z to 9999-12-31T23:59:59Z)"
        )
    return _EPOCH + timedelta(milliseconds=instant)


def format_iso_utc(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601 UTC with a trailing ``Z``.

    The inverse of :func:`parse_iso_datetime`, and the format the history-family
    tools return (``contract/tools.json`` -> ``resultShapes.historyRecords``).
    Previously these timestamps were ``str(datetime)`` — ``"2026-09-09 13:36:01.468000"``,
    space-separated and with no zone — which is not what the same tools *accept*
    for ``start_time``/``end_time``, and not what the Node server emitted.

    python-opcua decodes OPC UA DateTimes into naive datetimes that are already
    UTC, so a naive value is labelled UTC rather than reinterpreted as local time.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
