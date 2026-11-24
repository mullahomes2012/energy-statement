"""Turn usage into statement lines. Pure functions, no Home Assistant imports."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .const import KIND_ELECTRICITY, KIND_GAS, KIND_WATER, UNIT_KWH
from .periods import fmt_range

PENNY = Decimal("0.01")


def money(value: Decimal | float) -> Decimal:
    """Round to the penny."""
    return Decimal(str(value)).quantize(PENNY, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal | float) -> str:
    """£1,234.56."""
    return f"£{money(value):,.2f}"


def fmt_rate(value: Decimal, prefix: str = "", suffix: str = "") -> str:
    """Show a rate with 2 to 4 decimals, dropping trailing zeros past 2."""
    text = f"{value:.4f}".rstrip("0")
    if len(text.split(".")[1]) < 2:
        text = f"{value:.2f}"
    return f"{prefix}{text}{suffix}"


def _d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def price_for_day(prices: list[dict], day: date) -> int:
    """Index of the price entry in force on day (prices sorted by 'from')."""
    chosen = 0
    for idx, entry in enumerate(prices):
        if date.fromisoformat(entry["from"]) <= day:
            chosen = idx
    return chosen


def sort_prices(prices: list[dict]) -> list[dict]:
    return sorted(prices, key=lambda p: p["from"])


def segments(prices: list[dict], days: list[date]) -> list[tuple[int, list[date]]]:
    """Group consecutive days that share the same price entry."""
    out: list[tuple[int, list[date]]] = []
    for day in days:
        idx = price_for_day(prices, day)
        if out and out[-1][0] == idx:
            out[-1][1].append(day)
        else:
            out.append((idx, [day]))
    return out


def compute_charges(
    supply: dict, daily: dict[date, float], days: list[date]
) -> dict[str, Any]:
    """Price one supply for a period.

    supply: supply options (kind, charge_standing, prices, unit ...)
    daily: usage per day in m³ (gas/water) or kWh (electricity, gas in kWh)
    days: every day in the period
    """
    kind = supply["kind"]
    prices = sort_prices(supply["prices"])
    segs = segments(prices, days)
    split = len(segs) > 1
    lines: list[dict[str, Any]] = []
    notes: list[str] = []
    total_usage = Decimal(0)
    total_kwh = Decimal(0)

    for idx, seg_days in segs:
        price = prices[idx]
        usage = sum((_d(daily.get(d, 0)) for d in seg_days), Decimal(0))
        suffix = f" ({fmt_range(seg_days[0], seg_days[-1])})" if split else ""

        if kind == KIND_ELECTRICITY:
            kwh = usage.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            rate = _d(price["unit_rate"])
            lines.append(
                {
                    "text": f"Electricity used: {kwh:,.1f} kWh × {fmt_rate(rate, suffix='p')}{suffix}",
                    "amount": money(kwh * rate / 100),
                }
            )
            total_usage += kwh
            total_kwh += kwh

        elif kind == KIND_GAS:
            rate = _d(price["unit_rate"])
            if supply.get("unit") == UNIT_KWH:
                kwh = usage.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                total_usage += kwh
            else:
                m3 = usage.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                cf = _d(price.get("cf"))
                cv = _d(price.get("cv"))
                kwh = (m3 * cf * cv / Decimal("3.6")).quantize(
                    Decimal("0.1"), rounding=ROUND_HALF_UP
                )
                notes.append(
                    f"{m3:,.2f} m³ × {cf} correction × {cv} CV ÷ 3.6 = {kwh:,.1f} kWh{suffix}"
                )
                total_usage += m3
            lines.append(
                {
                    "text": f"Gas used: {kwh:,.1f} kWh × {fmt_rate(rate, suffix='p')}{suffix}",
                    "amount": money(kwh * rate / 100),
                }
            )
            total_kwh += kwh

        elif kind == KIND_WATER:
            m3 = usage.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            clean = _d(price["clean_rate"])
            waste = _d(price["waste_rate"])
            pct = _d(price.get("waste_pct", 100))
            lines.append(
                {
                    "text": f"Clean water: {m3:,.2f} m³ × {fmt_rate(clean, '£')}{suffix}",
                    "amount": money(m3 * clean),
                }
            )
            if waste:
                waste_m3 = (m3 * pct / 100).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                pct_txt = "" if pct == 100 else f" ({pct.normalize()}% of water used)"
                lines.append(
                    {
                        "text": f"Wastewater: {waste_m3:,.2f} m³ × {fmt_rate(waste, '£')}{pct_txt}{suffix}",
                        "amount": money(waste_m3 * waste),
                    }
                )
            total_usage += m3

        if supply.get("charge_standing"):
            standing = _d(price.get("standing"))
            if standing:
                lines.append(
                    {
                        "text": f"Standing charge: {len(seg_days)} days × {fmt_rate(standing, suffix='p')}{suffix}",
                        "amount": money(len(seg_days) * standing / 100),
                    }
                )

    subtotal = sum((line["amount"] for line in lines), Decimal(0))
    return {
        "usage": total_usage,
        "kwh": total_kwh,
        "lines": lines,
        "notes": notes,
        "subtotal": money(subtotal),
    }


def compute_fixed(supply: dict, days: list[date]) -> dict[str, Any]:
    """A fixed monthly charge with no meter.

    A whole calendar month is charged the full amount. A part month is charged
    the days used out of the days in that month (e.g. 10 of 30 days).
    """
    prices = sort_prices(supply["prices"])
    groups: dict[tuple[int, int, int], list[date]] = {}
    for day in days:
        groups.setdefault((day.year, day.month, price_for_day(prices, day)), []).append(day)
    lines: list[dict[str, Any]] = []
    for (year, month, idx), group in groups.items():
        monthly = _d(prices[idx]["monthly"])
        in_month = calendar.monthrange(year, month)[1]
        if len(group) == in_month:
            lines.append({"text": f"Fixed charge, {group[0].strftime('%B %Y')}", "amount": money(monthly)})
        else:
            lines.append(
                {
                    "text": (
                        f"Fixed charge, {fmt_range(group[0], group[-1])}: {fmt_money(monthly)} a month "
                        f"× {len(group)} of {in_month} days"
                    ),
                    "amount": money(monthly * len(group) / in_month),
                }
            )
    subtotal = sum((line["amount"] for line in lines), Decimal(0))
    return {"usage": Decimal(0), "kwh": Decimal(0), "lines": lines, "notes": [], "subtotal": money(subtotal)}
