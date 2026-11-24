"""Manual meter readings and pro-rating. Pure functions, no Home Assistant imports."""

from __future__ import annotations

from datetime import datetime


def parse_readings(raw: list[dict]) -> list[tuple[datetime, float]]:
    """Stored [{'t': iso, 'v': float}] to a sorted list of (datetime, value)."""
    return sorted((datetime.fromisoformat(r["t"]), float(r["v"])) for r in raw)


def value_at(
    readings: list[tuple[datetime, float]], when: datetime
) -> float | None:
    """Pro-rated meter value at a moment, by straight line between readings.

    Returns None when there is no reading at or after `when` (we never guess
    forwards), or no reading at or before it (we never guess backwards).
    """
    before = None
    after = None
    for t, v in readings:
        if t <= when:
            before = (t, v)
        if t >= when and after is None:
            after = (t, v)
    if before is None or after is None:
        return None
    (t1, v1), (t2, v2) = before, after
    if t2 == t1:
        return v1
    fraction = (when - t1).total_seconds() / (t2 - t1).total_seconds()
    return v1 + (v2 - v1) * fraction
