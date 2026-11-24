"""Render a statement to PDF with fpdf2. No Home Assistant imports."""

from __future__ import annotations

from datetime import date
from typing import Any

from fpdf import FPDF, XPos, YPos

from .periods import fmt_day, fmt_range

INK = (31, 30, 29)
MUTED = (110, 108, 102)
RULE = (214, 212, 205)
TILE = (244, 243, 239)
ACCENT_BG = (230, 241, 251)
ACCENT_INK = (12, 68, 124)
BAR = (133, 183, 235)

LEFT = 18
WIDTH = 174


def _t(text: Any) -> str:
    """Core PDF fonts are Latin-1 only; swap anything else out safely."""
    return (
        str(text)
        .replace("–", "-")
        .replace("—", "-")
        .replace("’", "'")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )


def _money(value: Any) -> str:
    return f"£{float(value):,.2f}"


class _Doc(FPDF):
    def footer(self) -> None:  # noqa: D401
        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 5, _t(f"Page {self.page_no()}"), align="R")


def statement_title(statement: dict) -> str:
    kinds = {s["kind"] for s in statement["supplies"]}
    if len(statement["supplies"]) == 1 and len(kinds) == 1:
        return f"{statement['supplies'][0]['kind_name']} usage statement"
    return "Utility usage statement"


def render_pdf(statement: dict, people: dict, payment: dict) -> bytes:
    """Build the PDF and return its bytes."""
    pdf = _Doc(format="A4")
    pdf.set_margins(LEFT, 16, LEFT)
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()
    start = date.fromisoformat(statement["start"])
    end = date.fromisoformat(statement["end"])
    supplies = statement["supplies"]
    single = len(supplies) == 1

    # Header
    top = pdf.get_y()
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(110, 9, _t(statement_title(statement)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*MUTED)
    address = people.get("address", "").replace("\n", ", ")
    sub = address if single else f"{address} · {fmt_range(start, end)}"
    pdf.multi_cell(110, 5, _t(sub), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    header_bottom = pdf.get_y()

    pdf.set_xy(LEFT + 110, top + 1)
    ref_label = "Preview - not a bill" if statement["type"] == "preview" else f"Statement no. {statement['reference']}"
    pdf.cell(WIDTH - 110, 5, _t(ref_label), align="R", new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_x(LEFT + 110)
    pdf.cell(WIDTH - 110, 5, _t(f"Issued {fmt_day(date.fromisoformat(statement['issued']))}"), align="R")
    pdf.set_y(max(header_bottom, top + 12) + 3)
    _rule(pdf)

    # Parties
    pdf.ln(3)
    y = pdf.get_y()
    for col, (label, name) in enumerate(
        (("From (landlord)", people.get("landlord_name", "")), ("To (tenant)", people.get("tenant_name", "")))
    ):
        pdf.set_xy(LEFT + col * (WIDTH / 2), y)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(WIDTH / 2, 5, _t(label), new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 11)
        pdf.set_text_color(*INK)
        pdf.cell(WIDTH / 2, 6, _t(name))
    pdf.set_y(y + 13)
    _rule(pdf)
    pdf.ln(5)

    # Summary tiles
    if single:
        s = supplies[0]
        tiles = [
            ("Period", fmt_range(start, end), False),
            ("Usage", "Fixed charge" if s.get("source") == "fixed"
             else f"{float(s['usage']):,.{s['usage_dp']}f} {s['usage_unit']}", False),
            ("Amount due", _money(statement["total"]), True),
        ]
    else:
        tiles = [(s["kind_name"], _money(s["subtotal"]), False) for s in supplies]
        tiles.append(("Total to pay", _money(statement["total"]), True))
    _tiles(pdf, tiles)
    pdf.ln(4)

    # Supply sections
    for s in supplies:
        if not single:
            pdf.set_font("Helvetica", "B", 11.5)
            pdf.set_text_color(*INK)
            pdf.cell(0, 7, _t(s["name"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        _readings(pdf, s)
        for note in s.get("notes", []):
            _row(pdf, note, "", muted=True)
        for idx, line in enumerate(s["lines"]):
            _row(pdf, line["text"], _money(line["amount"]))
        if not single:
            _row(pdf, f"{s['kind_name']} subtotal", _money(s["subtotal"]), bold=True)
        _rule(pdf)
        pdf.ln(2)

    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*INK)
    pdf.cell(WIDTH - 40, 8, "Total to pay")
    pdf.cell(40, 8, _t(_money(statement["total"])), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # Charts
    for s in supplies:
        if s.get("daily"):
            _chart(pdf, s, start, end)

    # Payment box
    if statement["type"] != "preview":
        _payment(pdf, statement, payment)

    return bytes(pdf.output())


def _rule(pdf: FPDF) -> None:
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.25)
    y = pdf.get_y()
    pdf.line(LEFT, y, LEFT + WIDTH, y)


def _tiles(pdf: FPDF, tiles: list[tuple[str, str, bool]]) -> None:
    gap = 3
    w = (WIDTH - gap * (len(tiles) - 1)) / len(tiles)
    y = pdf.get_y()
    for i, (label, value, accent) in enumerate(tiles):
        x = LEFT + i * (w + gap)
        pdf.set_fill_color(*(ACCENT_BG if accent else TILE))
        pdf.rect(x, y, w, 16, style="F", round_corners=True, corner_radius=2)
        pdf.set_xy(x + 3, y + 2.5)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*(ACCENT_INK if accent else MUTED))
        pdf.cell(w - 6, 4.5, _t(label), new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_x(x + 3)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(*(ACCENT_INK if accent else INK))
        pdf.cell(w - 6, 7, _t(value))
    pdf.set_y(y + 16)


def _row(pdf: FPDF, text: str, amount: str, muted: bool = False, bold: bool = False) -> None:
    pdf.set_font("Helvetica", "B" if bold else "", 9.5 if muted else 10)
    pdf.set_text_color(*(MUTED if muted else INK))
    if pdf.get_y() + 12 > pdf.h - 20:
        pdf.add_page()
    y = pdf.get_y()
    pdf.set_x(LEFT)
    pdf.multi_cell(WIDTH - 34, 5.5, _t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    after = pdf.get_y()
    pdf.set_xy(LEFT + WIDTH - 34, y)
    pdf.cell(34, 5.5, _t(amount), align="R")
    pdf.set_xy(LEFT, after + 0.5)


def _readings(pdf: FPDF, s: dict) -> None:
    unit = s["reading_unit"]
    dp = s["reading_dp"]
    if s.get("open") is not None:
        _row(pdf, f"Opening reading ({s['open_label']})", f"{float(s['open']):,.{dp}f} {unit}", muted=True)
    if s.get("close") is not None:
        _row(pdf, f"Closing reading ({s['close_label']})", f"{float(s['close']):,.{dp}f} {unit}", muted=True)


def _chart(pdf: FPDF, s: dict, start: date, end: date) -> None:
    daily = s["daily"]
    if pdf.get_y() + 40 > pdf.h - 30:
        pdf.add_page()
    values = [max(0.0, float(v)) for _, v in daily]
    peak = max(values) if values and max(values) > 0 else 1.0
    height = 22
    y0 = pdf.get_y() + 2
    gap = 0.8 if len(values) < 45 else 0.3
    bar_w = (WIDTH - gap * (len(values) - 1)) / max(1, len(values))
    pdf.set_fill_color(*BAR)
    for i, v in enumerate(values):
        h = max(0.3, height * v / peak)
        pdf.rect(LEFT + i * (bar_w + gap), y0 + height - h, bar_w, h, style="F")
    pdf.set_draw_color(*RULE)
    pdf.line(LEFT, y0 + height, LEFT + WIDTH, y0 + height)
    pdf.set_y(y0 + height + 1.5)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(
        WIDTH / 2, 4.5,
        _t(f"{s['name']} - daily usage ({s['usage_unit']}), {fmt_range(start, end)}"),
    )
    pdf.cell(WIDTH / 2, 4.5, _t(f"Highest day {peak:,.1f} {s['usage_unit']}"), align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)


def _payment(pdf: FPDF, statement: dict, payment: dict) -> None:
    rows = [("Amount", _money(statement["total"]))]
    if statement.get("due"):
        rows.append(("Pay by", fmt_day(date.fromisoformat(statement["due"]))))
    if payment.get("account_name"):
        rows.append(("Account name", payment["account_name"]))
    if payment.get("sort_code") or payment.get("account_number"):
        rows.append(("Sort code / account", f"{payment.get('sort_code', '')} / {payment.get('account_number', '')}"))
    rows.append(("Reference", statement["reference"]))
    note = payment.get("note") or ""
    box_h = 10 + 5.5 * len(rows) + (6 if note else 0)
    if pdf.get_y() + box_h > pdf.h - 22:
        pdf.add_page()
    y = pdf.get_y() + 2
    pdf.set_draw_color(*MUTED)
    pdf.set_line_width(0.3)
    pdf.rect(LEFT, y, WIDTH, box_h, round_corners=True, corner_radius=2)
    pdf.set_xy(LEFT + 4, y + 3)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*INK)
    pdf.cell(0, 6, "How to pay", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    for label, value in rows:
        pdf.set_x(LEFT + 4)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*MUTED)
        pdf.cell(50, 5.5, _t(label))
        pdf.set_text_color(*INK)
        pdf.cell(0, 5.5, _t(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if note:
        pdf.set_x(LEFT + 4)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 6, _t(note))
