"""Meter totals from Home Assistant's hourly statistics. No HA imports.

HA keeps one row per hour while a meter is online: (hour start, meter reading
at hour end, running total). While a meter is offline there are no rows; when
it comes back, everything it counted in the meantime arrives in the first row.
These helpers share such a jump out in proportion to time, so usage during an
outage is split fairly between days and billing periods, and every unit is
counted exactly once.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta

HOUR = timedelta(hours=1)

Row = tuple[datetime, float | None, float]  # (hour start, reading at hour end, running total)


def total_at(rows: list[Row], when: datetime) -> dict | None:
    """Running total (and meter reading) at a moment.

    Returns None if there's no data at all, {"offline_since": t} if the meter
    went offline before `when` and hasn't come back yet, otherwise
    {"total", "reading", "gap"} where gap is (went offline, came back) when
    the value had to be pro-rated across an outage.
    """
    if not rows:
        return None
    before = [r for r in rows if r[0] + HOUR <= when]
    after = [r for r in rows if r[0] + HOUR > when]
    if before and before[-1][0] + HOUR == when:
        return {"total": before[-1][2], "reading": before[-1][1], "gap": None}
    if not before:
        # Meter started recording after `when`: count from its first hour.
        first = after[0]
        return {"total": first[2], "reading": first[1], "gap": None, "started": first[0]}
    last = before[-1]
    offline_from = last[0] + HOUR
    if not after:
        return {"offline_since": offline_from}
    back = after[0]
    back_at = back[0] + HOUR
    fraction = (when - offline_from) / (back_at - offline_from)
    total = last[2] + (back[2] - last[2]) * fraction
    reading = None
    if last[1] is not None and back[1] is not None and back[1] >= last[1]:
        reading = last[1] + (back[1] - last[1]) * fraction
    return {"total": total, "reading": reading, "gap": (offline_from, back[0])}


def latest_total(rows: list[Row], until: datetime) -> dict | None:
    """Running total at the last complete hour before `until` (for live figures)."""
    usable = [r for r in rows if r[0] + HOUR <= until]
    if not usable:
        return None
    last = usable[-1]
    return {"total": last[2], "reading": last[1], "gap": None, "at": last[0] + HOUR}


def daily_usage(
    rows: list[Row],
    start: datetime,
    start_total: float,
    end: datetime,
    end_total: float,
    local_date: Callable[[datetime], date],
) -> dict[date, float]:
    """Usage per local day, with any outage spread evenly over its hours."""
    points = [(start, start_total)]
    points += [(r[0] + HOUR, r[2]) for r in rows if start < r[0] + HOUR < end]
    points.append((end, end_total))
    daily: dict[date, float] = {}
    for (t_a, s_a), (t_b, s_b) in zip(points, points[1:]):
        delta = s_b - s_a
        span = (t_b - t_a).total_seconds()
        if span <= 0 or abs(delta) < 1e-12:
            continue
        cursor = t_a
        while cursor < t_b:
            piece_end = min(cursor + HOUR, t_b)
            day = local_date(cursor)
            daily[day] = daily.get(day, 0.0) + delta * (piece_end - cursor).total_seconds() / span
            cursor = piece_end
    return daily
