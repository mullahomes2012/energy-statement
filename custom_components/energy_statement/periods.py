"""Billing period maths. Pure functions, no Home Assistant imports."""

from __future__ import annotations

from datetime import date, timedelta

from .const import CYCLE_MONTHLY, CYCLE_QUARTERLY, CYCLE_WEEKLY


def add_months(day: date, months: int) -> date:
    """Add months to a date whose day is 28 or less."""
    years, month0 = divmod(day.month - 1 + months, 12)
    return date(day.year + years, month0 + 1, day.day)


def period_containing(
    day: date, cycle: str, start_day: int = 1, start_weekday: int = 0
) -> tuple[date, date]:
    """Return (start, end_exclusive) of the billing period that contains day.

    Monthly periods start on start_day each month.
    Quarterly periods start on start_day of Jan, Apr, Jul and Oct.
    Weekly periods start on start_weekday (0 = Monday).
    """
    start_day = max(1, min(28, int(start_day)))
    if cycle == CYCLE_WEEKLY:
        start = day - timedelta(days=(day.weekday() - int(start_weekday)) % 7)
        return start, start + timedelta(days=7)

    if cycle == CYCLE_QUARTERLY:
        first_month = day.month - ((day.month - 1) % 3)
        start = date(day.year, first_month, start_day)
        if start > day:
            start = add_months(start, -3)
        return start, add_months(start, 3)

    # Monthly (default)
    start = date(day.year, day.month, start_day)
    if start > day:
        start = add_months(start, -1)
    return start, add_months(start, 1)


def days_between(start: date, end_exclusive: date) -> list[date]:
    """Every day from start up to, not including, end_exclusive."""
    return [start + timedelta(days=i) for i in range((end_exclusive - start).days)]


def fmt_day(day: date) -> str:
    """1 Oct 2026."""
    return f"{day.day} {day.strftime('%b %Y')}"


def fmt_range(start: date, end_inclusive: date) -> str:
    """1 to 31 Oct 2026 / 28 Sep to 3 Oct 2026 / 15 Dec 2026 to 14 Jan 2027."""
    if start.year != end_inclusive.year:
        return f"{fmt_day(start)} to {fmt_day(end_inclusive)}"
    if start.month != end_inclusive.month:
        return f"{start.day} {start.strftime('%b')} to {fmt_day(end_inclusive)}"
    if start == end_inclusive:
        return fmt_day(start)
    return f"{start.day} to {fmt_day(end_inclusive)}"
