"""Statement manager: schedule, usage, approval, sending."""

from __future__ import annotations

import asyncio
import copy
import html as html_lib
import logging
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_APPROVE,
    ACTION_READING,
    ACTION_SKIP,
    DOMAIN,
    FT3_100_TO_M3,
    KIND_ELECTRICITY,
    KIND_GAS,
    KIND_NAMES,
    MODE_AUTO,
    MODE_MANUAL,
    OPT_EMAIL,
    OPT_PAYMENT,
    OPT_PEOPLE,
    OPT_SCHEDULE,
    OPT_SUPPLIES,
    PDF_URL_BASE,
    SOURCE_FIXED,
    SOURCE_MANUAL,
    SOURCE_SENSOR,
    STATUS_APPROVAL,
    STATUS_IDLE,
    STATUS_WAITING,
    STORAGE_VERSION,
    UNIT_FT3,
    UNIT_KWH,
    UPDATE_MINUTES,
)
from .mailer import MailError, send_mail
from .pdf import render_pdf, statement_title
from .periods import days_between, fmt_day, fmt_range, period_containing
from .pricing import compute_charges, compute_fixed, fmt_money, money
from .meter import daily_usage, latest_total, total_at
from .readings import parse_readings, value_at

_LOGGER = logging.getLogger(__name__)

DEFAULT_STORE: dict[str, Any] = {
    "counter": 0,
    "last_period_end": None,
    "pending": None,
    "last": None,
    "billed_to": {},
    "readings": {},
    "last_reminded": None,
}


class StatementManager:
    """Everything one config entry (one property) needs."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.data: dict[str, Any] = copy.deepcopy(DEFAULT_STORE)
        self.inputs: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []
        self.coordinator: DataUpdateCoordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=UPDATE_MINUTES),
            update_method=self._async_update_data,
        )

    # ------------------------------------------------------------------ config
    @property
    def opts(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def supplies(self) -> dict[str, dict]:
        return self.opts.get(OPT_SUPPLIES, {})

    @property
    def schedule(self) -> dict[str, Any]:
        return self.opts.get(OPT_SCHEDULE, {})

    @property
    def people(self) -> dict[str, Any]:
        return self.opts.get(OPT_PEOPLE, {})

    @property
    def payment(self) -> dict[str, Any]:
        return self.opts.get(OPT_PAYMENT, {})

    @property
    def email(self) -> dict[str, Any]:
        return self.opts.get(OPT_EMAIL, {})

    def missing_setup(self) -> list[str]:
        """Details still needed before a statement can be emailed."""
        missing: list[str] = []
        if self.schedule.get("sending_mode") == MODE_MANUAL:
            return missing
        if not self.people.get("tenant_email"):
            missing.append("tenant's email (People and property)")
        if not self.email.get("smtp_host"):
            missing.append("email sending (Email sending)")
        return missing

    def manual_supplies(self) -> dict[str, dict]:
        return {k: v for k, v in self.supplies.items() if v.get("source") == SOURCE_MANUAL}

    @property
    def send_time(self) -> time:
        return time.fromisoformat(self.schedule.get("send_time", "09:00:00"))

    def period_for(self, day: date) -> tuple[date, date]:
        sch = self.schedule
        return period_containing(
            day, sch.get("cycle", "monthly"), int(sch.get("start_day", 1)), int(sch.get("start_weekday", 0))
        )

    @property
    def status(self) -> str:
        pending = self.data.get("pending")
        return pending["status"] if pending else STATUS_IDLE

    def next_statement_at(self) -> datetime | None:
        if not self.data.get("last_period_end"):
            return None
        start = date.fromisoformat(self.data["last_period_end"])
        end = self.period_for(start)[1]
        return self._at_send_time(end)

    def _at_send_time(self, day: date) -> datetime:
        t = self.send_time
        return dt_util.start_of_local_day(day) + timedelta(hours=t.hour, minutes=t.minute)

    # ------------------------------------------------------------------ setup
    async def async_setup(self) -> None:
        stored = await self.store.async_load()
        if stored:
            self.data.update(stored)
        today = dt_util.now().date()
        billing_start = self.schedule.get("billing_start")
        if billing_start and self.data.get("billing_start_applied") != billing_start and not self.data.get("pending"):
            # New or changed "Billing starts on" date: bill from that day.
            self.data["last_period_end"] = billing_start
            self.data["billing_start_applied"] = billing_start
        if not self.data.get("last_period_end"):
            # No start date set: bill from the current period; earlier days
            # can be sent as custom statements.
            self.data["last_period_end"] = self.period_for(today)[0].isoformat()
        for sid, sup in self.manual_supplies().items():
            initial = sup.get("initial_reading")
            if initial and not self.data["readings"].get(sid):
                self.data["readings"][sid] = [initial]
        last_month_end = self.period_for(today)[0] - timedelta(days=1)
        self.inputs.setdefault("custom_start", self.period_for(last_month_end)[0])
        self.inputs.setdefault("custom_end", last_month_end)
        for sid in self.manual_supplies():
            self.inputs.setdefault(f"reading_date_{sid}", today)
            self.inputs.setdefault(f"reading_{sid}", None)
        await self._save()

        if missing := self.missing_setup():
            persistent_notification.async_create(
                self.hass,
                "Statements will be worked out as normal but can't be emailed until you add: "
                + ", ".join(missing)
                + ".\n\nGo to Settings → Devices and services → Energy Statement → Configure. "
                "Payment details are optional but recommended.",
                title=f"{self.entry.title}: finish setting up",
                notification_id=self._nid("setup"),
            )
        else:
            persistent_notification.async_dismiss(self.hass, self._nid("setup"))

        t = self.send_time
        self._unsubs.append(
            async_track_time_change(self.hass, self._timer, hour=t.hour, minute=t.minute, second=0)
        )
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._mobile_action)
        )

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _save(self) -> None:
        await self.store.async_save(self.data)

    def _refresh(self) -> None:
        self.coordinator.async_update_listeners()
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    # ------------------------------------------------------------------ usage
    def _stat_units(self, supply: dict) -> dict[str, str]:
        if supply["kind"] == KIND_ELECTRICITY or supply.get("unit") == UNIT_KWH:
            return {"energy": "kWh"}
        return {"volume": "m³"}

    async def _stat(self, func, *args) -> Any:
        return await get_instance(self.hass).async_add_executor_job(func, self.hass, *args)

    async def _hour_rows(
        self, entity_id: str, start: datetime, end: datetime, units: dict
    ) -> list[tuple[datetime, float | None, float]]:
        """Hourly statistics as (hour start, meter reading at hour end, running sum)."""
        rows = await self._stat(
            statistics_during_period, start, end, {entity_id}, "hour", units, {"state", "sum"}
        )
        out = []
        for row in rows.get(entity_id) or []:
            begin = row["start"]
            begin = dt_util.utc_from_timestamp(begin) if isinstance(begin, (int, float)) else begin
            state = row.get("state")
            out.append((begin, float(state) if state is not None else None, float(row.get("sum") or 0)))
        return out

    async def _sensor_usage(
        self, sid: str, supply: dict, start_dt: datetime, end_dt: datetime, stype: str
    ) -> tuple[dict | None, str | None]:
        """Return (usage, missing_reason) for a sensor supply.

        Usage is the running total at the end minus the running total at the
        start, from HA's hourly statistics. If the meter was offline across
        either point, the outage is shared out in proportion to time.
        """
        entity_id = supply["sensor"]
        name = supply.get("name") or KIND_NAMES[supply["kind"]]
        units = self._stat_units(supply)
        now = dt_util.now()
        rows = await self._hour_rows(
            entity_id, start_dt - timedelta(days=60), min(end_dt + timedelta(days=60), now), units
        )
        begin = total_at(rows, start_dt)
        if begin is None or "offline_since" in begin:
            if stype == "preview" and begin and "offline_since" in begin:
                return None, f"{name}: the meter has been offline since {_when(begin['offline_since'])}"
            if begin is None:
                return None, f"{name}: no data recorded from {entity_id}"
            return None, (
                f"{name}: the meter has been offline since {_when(begin['offline_since'])}; "
                "the statement continues once it reconnects"
            )
        if stype == "preview":
            finish = latest_total(rows, min(end_dt, now))
            if finish is None:
                return None, f"{name}: no data recorded from {entity_id} yet"
            end_point = finish["at"]
        else:
            finish = total_at(rows, end_dt)
            if finish is None:
                return None, f"{name}: no data recorded from {entity_id}"
            if "offline_since" in finish:
                return None, (
                    f"{name}: the meter has been offline since {_when(finish['offline_since'])}; "
                    "the statement continues once it reconnects"
                )
            end_point = end_dt
        if begin.get("started") and begin["started"] >= end_point:
            return None, f"{name}: no data recorded from {entity_id} in this period"

        usage = max(0.0, finish["total"] - begin["total"])
        daily = daily_usage(
            rows, start_dt, begin["total"], end_point, finish["total"],
            lambda t: dt_util.as_local(t).date(),
        )
        notes: list[str] = []
        for point, label in ((begin, "start"), (finish, "end")):
            if point.get("gap"):
                a, b = point["gap"]
                notes.append(
                    f"The meter was offline from {_when(a)} to {_when(b)}. Usage during that time is "
                    "shared out evenly over those hours, so each statement only includes its own share."
                )
        if begin.get("started"):
            notes.append(f"The meter started recording on {_when(begin['started'])}.")
        return {
            "usage": usage,
            "daily": daily,
            "open": begin.get("reading"),
            "close": finish.get("reading"),
            "open_estimated": bool(begin.get("gap")),
            "close_estimated": bool(finish.get("gap")),
            "notes": list(dict.fromkeys(notes)),
        }, None

    def _manual_usage(
        self, sid: str, supply: dict, start_dt: datetime, end_dt: datetime, stype: str
    ) -> tuple[dict | None, str | None]:
        """Return (usage, missing_reason)."""
        readings = parse_readings(self.data["readings"].get(sid, []))
        name = supply.get("name") or KIND_NAMES[supply["kind"]]
        if not readings:
            return None, f"{name}: no meter readings entered yet"
        notes: list[str] = []
        start_day = start_dt.date()

        billed = self.data["billed_to"].get(sid)
        if stype == "regular" and billed and billed.get("date") == start_day.isoformat():
            v_start = float(billed["value"])
        else:
            v_start = value_at(readings, start_dt)
        open_label = f"{fmt_day(start_day)}, pro-rated"
        if v_start is None:
            first_t, first_v = readings[0]
            if start_dt < first_t < end_dt:
                v_start = first_v
                open_label = f"{fmt_day(first_t.date())}, first reading"
                notes.append(f"No reading before {fmt_day(first_t.date())}, so usage is counted from the first reading.")
            elif first_t >= end_dt:
                return None, f"{name}: the first reading is after this period"
        if any(t == start_dt for t, _ in readings):
            open_label = fmt_day(start_day)

        v_end = value_at(readings, end_dt)
        close_label = f"{fmt_day(end_dt.date())}, pro-rated"
        if v_end is None:
            if stype != "preview":
                return None, f"{name}: needs a reading taken on or after {fmt_day(end_dt.date())}"
            last_t, last_v = readings[-1]
            v_end = last_v if last_t > start_dt else v_start
            close_label = f"last reading {fmt_day(last_t.date())}"
        elif any(t == end_dt for t, _ in readings):
            close_label = fmt_day(end_dt.date())
        if v_start is None:
            return None, f"{name}: needs a reading taken on or before {fmt_day(start_day)}"
        usage = v_end - v_start
        if usage < 0:
            return None, f"{name}: readings go backwards; check the entered readings"
        priced = usage
        if supply["kind"] == KIND_GAS and supply.get("unit") == UNIT_FT3:
            priced = usage * FT3_100_TO_M3
            notes.append(f"{usage:,.2f} hundred ft³ × 2.83168 = {priced:,.2f} m³")
        return {
            "usage": priced,
            "open": v_start,
            "close": v_end,
            "open_label": open_label,
            "close_label": close_label,
            "notes": notes,
            "end_value": v_end,
        }, None

    # ------------------------------------------------------------------ build
    async def _build(
        self, start: date, end_excl: date, stype: str
    ) -> tuple[dict | None, list[str], dict]:
        """Build a statement. Returns (statement, missing, billed_to_updates)."""
        start_dt = dt_util.start_of_local_day(start)
        end_dt = dt_util.start_of_local_day(end_excl)
        if stype == "preview":
            end_dt = min(end_dt, dt_util.now())
        last_day = dt_util.as_local(end_dt - timedelta(seconds=1)).date()
        days = days_between(start, last_day + timedelta(days=1))

        out: list[dict] = []
        missing: list[str] = []
        billed: dict[str, dict] = {}
        for sid, sup in self.supplies.items():
            kind = sup["kind"]
            name = sup.get("name") or KIND_NAMES[kind]
            unit_label, reading_unit, reading_dp, usage_dp = _units(sup)
            entry: dict[str, Any] = {
                "id": sid, "kind": kind, "kind_name": KIND_NAMES[kind], "name": name,
                "source": sup.get("source"), "usage_unit": unit_label, "usage_dp": usage_dp,
                "reading_unit": reading_unit, "reading_dp": reading_dp, "daily": [],
            }
            if sup.get("source") == SOURCE_MANUAL:
                usage, reason = self._manual_usage(sid, sup, start_dt, end_dt, stype)
                if usage is None:
                    missing.append(reason or name)
                    continue
                per_day = usage["usage"] / max(1, len(days))
                priced = compute_charges(sup, {d: per_day for d in days}, days)
                entry.update(
                    open=usage["open"], close=usage["close"],
                    open_label=usage["open_label"], close_label=usage["close_label"],
                )
                notes = usage["notes"] + priced["notes"]
                billed[sid] = {"date": end_excl.isoformat(), "value": usage["end_value"]}
            elif sup.get("source") == SOURCE_FIXED:
                priced = compute_fixed(sup, days)
                notes = []
            else:
                usage, reason = await self._sensor_usage(sid, sup, start_dt, end_dt, stype)
                if usage is None:
                    missing.append(reason or name)
                    continue
                priced = compute_charges(sup, usage["daily"], days)
                if stype == "preview":
                    close_label = "now"
                else:
                    close_label = f"{fmt_day(end_excl)} 00:00" + (", pro-rated" if usage["close_estimated"] else "")
                open_label = f"{fmt_day(start)} 00:00" + (", pro-rated" if usage["open_estimated"] else "")
                entry.update(
                    open=usage["open"], close=usage["close"],
                    open_label=open_label, close_label=close_label,
                    daily=[[d.isoformat(), round(v, 3)] for d, v in sorted(usage["daily"].items())],
                )
                notes = usage["notes"] + priced["notes"]
                if (
                    not usage["open_estimated"] and not usage["close_estimated"]
                    and usage["open"] is not None and usage["close"] is not None
                    and abs((usage["close"] - usage["open"]) - usage["usage"]) > 0.5
                ):
                    notes = notes + ["The meter's counter was reset during this period; usage is taken from the recorded history."]
            entry.update(
                usage=str(priced["usage"]),
                lines=[{"text": l["text"], "amount": str(l["amount"])} for l in priced["lines"]],
                notes=notes,
                subtotal=str(priced["subtotal"]),
            )
            out.append(entry)

        if missing and stype != "preview":
            return None, missing, {}
        if not out:
            return None, missing, {}
        total = money(sum((Decimal(s["subtotal"]) for s in out), Decimal(0)))
        today = dt_util.now().date()
        statement = {
            "type": stype,
            "number": None,
            "reference": "PREVIEW",
            "start": start.isoformat(),
            "end": last_day.isoformat(),
            "end_excl": end_excl.isoformat(),
            "issued": today.isoformat(),
            "due": None,
            "total": str(total),
            "supplies": out,
            "skipped": missing,
        }
        return statement, missing, billed

    def _date_statement(self, statement: dict) -> None:
        today = dt_util.now().date()
        statement["issued"] = today.isoformat()
        days = int(self.payment.get("pay_within_days", 14) or 0)
        statement["due"] = (today + timedelta(days=days)).isoformat() if days else None

    async def _write_pdf(self, statement: dict) -> str:
        filename = "preview.pdf" if statement["type"] == "preview" else f"{statement['reference']}.pdf"
        folder = self.hass.config.path(DOMAIN, self.entry.entry_id)

        def _do() -> None:
            os.makedirs(folder, exist_ok=True)
            data = render_pdf(statement, self.people, self.payment)
            with open(os.path.join(folder, filename), "wb") as fh:
                fh.write(data)

        await self.hass.async_add_executor_job(_do)
        statement["pdf"] = filename
        return filename

    def pdf_path(self, filename: str) -> str:
        return self.hass.config.path(DOMAIN, self.entry.entry_id, filename)

    def pdf_link(self, filename: str) -> str:
        return async_sign_path(
            self.hass, f"{PDF_URL_BASE}/{self.entry.entry_id}/{filename}", timedelta(days=30)
        )

    # ------------------------------------------------------------------ schedule
    async def _timer(self, now: datetime) -> None:
        await self.async_check(from_timer=True)

    async def async_check(self, from_timer: bool = False) -> None:
        async with self._lock:
            try:
                await self._check(from_timer)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Statement check failed")

    async def _check(self, from_timer: bool) -> None:
        pending = self.data.get("pending")
        if pending:
            if pending["status"] == STATUS_WAITING and pending.get("type") == "regular":
                await self._try_regular(
                    date.fromisoformat(pending["start"]), date.fromisoformat(pending["end_excl"]), from_timer
                )
            return
        start = date.fromisoformat(self.data["last_period_end"])
        end = self.period_for(start)[1]
        if dt_util.now() < self._at_send_time(end):
            return
        await self._try_regular(start, end, from_timer)

    async def _try_regular(self, start: date, end: date, remind: bool) -> None:
        statement, missing, billed = await self._build(start, end, "regular")
        if statement is None:
            first = not self.data.get("pending")
            today = dt_util.now().date().isoformat()
            self.data["pending"] = {
                "status": STATUS_WAITING, "type": "regular",
                "start": start.isoformat(), "end_excl": end.isoformat(), "missing": missing,
            }
            if first or (remind and self.data.get("last_reminded") != today):
                self.data["last_reminded"] = today
                await self._notify_missing(start, end, missing)
            await self._save()
            self._refresh()
            return
        statement["billed"] = billed
        await self._ready(statement)

    async def _ready(self, statement: dict) -> None:
        self.data["counter"] = int(self.data.get("counter", 0)) + 1
        statement["number"] = self.data["counter"]
        prefix = (self.people.get("reference_prefix") or "").strip()
        num = f"{statement['number']:04d}"
        statement["reference"] = f"{prefix}-{num}" if prefix else num
        self._date_statement(statement)
        await self._write_pdf(statement)
        persistent_notification.async_dismiss(self.hass, self._nid("missing"))

        if self.schedule.get("sending_mode") == MODE_MANUAL:
            await self._finalise(statement, "to hand over")
            await self._notify_print(statement)
            return
        if missing := self.missing_setup():
            statement["error"] = "Not set up for email yet. Add: " + ", ".join(missing) + "."
        elif self.schedule.get("sending_mode", MODE_AUTO) == MODE_AUTO:
            try:
                await self._send(statement)
                return
            except MailError as err:
                statement["error"] = str(err)
        self.data["pending"] = {"status": STATUS_APPROVAL, "type": statement["type"], "statement": statement}
        await self._save()
        await self._notify_approval(statement)
        self._refresh()

    async def _send(self, statement: dict) -> None:
        cfg = self.email
        people = self.people
        to = [people["tenant_email"]]
        cc = [people["landlord_email"]] if people.get("landlord_email") else []
        subject, text, html = self._email_content(statement)
        pdf_bytes = await self.hass.async_add_executor_job(_read_file, self.pdf_path(statement["pdf"]))
        await self.hass.async_add_executor_job(
            send_mail, cfg, to, cc, subject, text, html, (statement["pdf"], pdf_bytes)
        )
        await self._finalise(statement, "sent")
        persistent_notification.async_dismiss(self.hass, self._nid("approval"))
        _LOGGER.info("Sent statement %s", statement["reference"])

    async def _finalise(self, statement: dict, status: str) -> None:
        if statement["type"] == "regular":
            self.data["last_period_end"] = statement["end_excl"]
            self.data["billed_to"].update(statement.get("billed", {}))
        self.data["last"] = {
            "reference": statement["reference"],
            "total": statement["total"],
            "start": statement["start"],
            "end": statement["end"],
            "issued": statement["issued"],
            "due": statement.get("due"),
            "type": statement["type"],
            "status": status,
            "pdf": statement.get("pdf"),
            "at": dt_util.now().isoformat(),
            "supplies": {s["name"]: s["subtotal"] for s in statement["supplies"]},
        }
        self.data["pending"] = None
        await self._save()
        self._refresh()
        if statement["type"] == "regular":
            # Catch up if more than one period is overdue.
            self.hass.async_create_task(self.async_check())

    # ------------------------------------------------------------------ actions
    async def async_approve(self) -> None:
        async with self._lock:
            pending = self.data.get("pending")
            if not pending or pending["status"] != STATUS_APPROVAL:
                raise HomeAssistantError("There's no statement waiting for approval.")
            if missing := self.missing_setup():
                raise HomeAssistantError(
                    "Can't send yet. Add these in Configure first: " + ", ".join(missing) + "."
                )
            statement = pending["statement"]
            statement.pop("error", None)
            self._date_statement(statement)
            await self._write_pdf(statement)
            try:
                await self._send(statement)
            except MailError as err:
                await self._save()
                raise HomeAssistantError(f"Statement not sent. {err}") from err

    async def async_mark_handed_over(self) -> None:
        """Close a waiting statement without emailing it (printed instead)."""
        async with self._lock:
            pending = self.data.get("pending")
            if not pending or pending["status"] != STATUS_APPROVAL:
                raise HomeAssistantError("There's no finished statement waiting to be sent.")
            statement = pending["statement"]
            statement.pop("error", None)
            await self._finalise(statement, "handed over")
            persistent_notification.async_dismiss(self.hass, self._nid("approval"))

    async def _notify_print(self, statement: dict) -> None:
        link = self.pdf_link(statement["pdf"])
        period = fmt_range(date.fromisoformat(statement["start"]), date.fromisoformat(statement["end"]))
        title = f"{self.entry.title}: statement ready to print"
        body = f"{statement['reference']} for {period}: {fmt_money(statement['total'])}."
        persistent_notification.async_create(
            self.hass,
            body + f"\n\n{_pdf_anchor(link, 'Open the PDF to print')}",
            title=title,
            notification_id=self._nid("print"),
        )
        await self._mobile(title, body + " Tap to open the PDF.", "print", url=link)

    async def async_discard(self) -> None:
        async with self._lock:
            pending = self.data.get("pending")
            if not pending:
                raise HomeAssistantError("There's no pending statement to discard.")
            if pending["status"] == STATUS_APPROVAL:
                statement = pending["statement"]
                if statement.get("number") == self.data.get("counter"):
                    self.data["counter"] -= 1
                statement["reference"] = f"{statement['reference']} (discarded)"
                await self._finalise(statement, "discarded")
            else:
                self.data["last_period_end"] = pending["end_excl"]
                self.data["pending"] = None
                await self._save()
                self._refresh()
            for tag in ("approval", "missing"):
                persistent_notification.async_dismiss(self.hass, self._nid(tag))

    async def async_custom(self, start: date, end_inclusive: date) -> None:
        if end_inclusive < start:
            raise HomeAssistantError("The end date is before the start date.")
        async with self._lock:
            if self.data.get("pending"):
                raise HomeAssistantError(
                    "Another statement is pending. Approve or discard it first."
                )
            statement, missing, _ = await self._build(start, end_inclusive + timedelta(days=1), "custom")
            if statement is None:
                raise HomeAssistantError("Can't create the statement: " + "; ".join(missing))
            await self._ready(statement)

    async def async_preview(self) -> str:
        today = dt_util.now().date()
        start, end = self.period_for(today)
        statement, missing, _ = await self._build(start, end, "preview")
        if statement is None:
            raise HomeAssistantError("Nothing to preview yet: " + "; ".join(missing))
        self._date_statement(statement)
        filename = await self._write_pdf(statement)
        link = self.pdf_link(filename)
        extra = ("\n\nNot included: " + "; ".join(missing)) if missing else ""
        persistent_notification.async_create(
            self.hass,
            f"{fmt_range(start, date.fromisoformat(statement['end']))} so far: "
            f"**{fmt_money(statement['total'])}**.\n\n{_pdf_anchor(link, 'Open the preview PDF')}{extra}",
            title=f"{self.entry.title}: statement preview",
            notification_id=self._nid("preview"),
        )
        return link

    async def async_add_reading(self, sid: str, value: float, day: date | None = None) -> None:
        supply = self.manual_supplies().get(sid)
        if supply is None:
            raise HomeAssistantError(f"{sid} isn't a manual-reading supply.")
        now = dt_util.now()
        if day is None or day >= now.date():
            when = now
        else:
            when = dt_util.start_of_local_day(day) + timedelta(hours=12)
        readings = parse_readings(self.data["readings"].get(sid, []))
        earlier = [v for t, v in readings if t <= when]
        later = [v for t, v in readings if t > when]
        if earlier and value < earlier[-1]:
            raise HomeAssistantError(
                f"{value:,} is lower than the previous reading ({earlier[-1]:,}). Check the number."
            )
        if later and value > later[0]:
            raise HomeAssistantError(
                f"{value:,} is higher than a later reading ({later[0]:,}). Check the date."
            )
        self.data["readings"].setdefault(sid, []).append({"t": when.isoformat(), "v": float(value)})
        self.data["readings"][sid].sort(key=lambda r: r["t"])
        self.inputs[f"reading_date_{sid}"] = now.date()
        self.inputs[f"reading_{sid}"] = None
        await self._save()
        self._refresh()
        if self.data.get("pending", {}) and self.data["pending"]["status"] == STATUS_WAITING:
            self.hass.async_create_task(self.async_check())

    async def async_delete_last_reading(self, sid: str) -> None:
        readings = self.data["readings"].get(sid) or []
        if not readings:
            raise HomeAssistantError("There are no readings to delete.")
        readings.pop()
        await self._save()
        self._refresh()

    def last_reading(self, sid: str) -> tuple[datetime, float] | None:
        readings = parse_readings(self.data["readings"].get(sid, []))
        return readings[-1] if readings else None

    # ------------------------------------------------------------------ notify
    def _nid(self, tag: str) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_{tag}"

    async def _mobile(self, title: str, message: str, tag: str, actions: list | None = None, url: str | None = None) -> None:
        target = self.schedule.get("notify_service")
        if not target:
            return
        service = target.split(".", 1)[-1]
        data: dict[str, Any] = {"tag": self._nid(tag)}
        if actions:
            data["actions"] = actions
        if url:
            data["url"] = url
            data["clickAction"] = url
        try:
            await self.hass.services.async_call(
                "notify", service, {"title": title, "message": message, "data": data}, blocking=True
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't send phone notification via %s: %s", target, err)

    async def _notify_missing(self, start: date, end: date, missing: list[str]) -> None:
        period = fmt_range(start, end - timedelta(days=1))
        title = f"{self.entry.title}: statement waiting"
        body = f"The statement for {period} is waiting for:\n" + "\n".join(f"- {m}" for m in missing)
        persistent_notification.async_create(
            self.hass,
            body + "\n\nAdd the reading on the integration's device page; the statement continues automatically.",
            title=title,
            notification_id=self._nid("missing"),
        )
        actions = []
        for sid, sup in self.manual_supplies().items():
            name = sup.get("name") or KIND_NAMES[sup["kind"]]
            if any(m.startswith(f"{name}:") for m in missing):
                last = self.last_reading(sid)
                actions.append(
                    {
                        "action": f"{ACTION_READING}_{self.entry.entry_id}_{sid}",
                        "title": f"Enter {name.lower()} reading",
                        "behavior": "textInput",
                        "textInputButtonTitle": "Save",
                        "textInputPlaceholder": f"Last: {last[1]:,}" if last else "Meter reading",
                    }
                )
        await self._mobile(title, body, "missing", actions[:3] or None)

    async def _notify_approval(self, statement: dict) -> None:
        link = self.pdf_link(statement["pdf"])
        period = fmt_range(date.fromisoformat(statement["start"]), date.fromisoformat(statement["end"]))
        title = f"{self.entry.title}: statement ready"
        err = f"\n\nAutomatic sending failed: {statement['error']}" if statement.get("error") else ""
        body = (
            f"{statement['reference']} for {period}: {fmt_money(statement['total'])}. "
            f"Approve to email it to the tenant and landlord.{err}"
        )
        persistent_notification.async_create(
            self.hass,
            body + f"\n\n{_pdf_anchor(link, 'Open the PDF')}\n\nOn the device page: *Approve and send* to email it, "
            "*Mark as handed over* if you've printed it instead, or *Discard*.",
            title=title,
            notification_id=self._nid("approval"),
        )
        await self._mobile(
            title, body, "approval",
            [
                {"action": f"{ACTION_APPROVE}_{self.entry.entry_id}", "title": "Approve and send"},
                {"action": f"{ACTION_SKIP}_{self.entry.entry_id}", "title": "Discard"},
            ],
            url=link,
        )

    @callback
    def _mobile_action(self, event: Event) -> None:
        action = str(event.data.get("action", ""))
        eid = self.entry.entry_id
        if action == f"{ACTION_APPROVE}_{eid}":
            self.hass.async_create_task(self._safe(self.async_approve()))
        elif action == f"{ACTION_SKIP}_{eid}":
            self.hass.async_create_task(self._safe(self.async_discard()))
        elif action.startswith(f"{ACTION_READING}_{eid}_"):
            sid = action.rsplit("_", 1)[-1]
            text = str(event.data.get("reply_text", "")).replace(",", "").strip()
            try:
                value = float(text)
            except ValueError:
                self.hass.async_create_task(
                    self._mobile(self.entry.title, f"'{text}' isn't a number. Reading not saved.", "missing")
                )
                return
            self.hass.async_create_task(self._safe(self.async_add_reading(sid, value)))

    async def _safe(self, coro) -> None:
        try:
            await coro
        except HomeAssistantError as err:
            await self._mobile(self.entry.title, str(err), "error")
            persistent_notification.async_create(
                self.hass, str(err), title=f"{self.entry.title}: problem", notification_id=self._nid("error")
            )

    # ------------------------------------------------------------------ email
    def _email_content(self, statement: dict) -> tuple[str, str, str]:
        people, pay = self.people, self.payment
        title = statement_title(statement)
        period = fmt_range(date.fromisoformat(statement["start"]), date.fromisoformat(statement["end"]))
        address = (people.get("address") or "").replace("\n", ", ")
        first_line = address.split(",")[0]
        total = fmt_money(statement["total"])
        subject = f"{title} for {first_line}, {period}: {total}"

        rows = [
            (
                s["name"],
                "Fixed charge" if s.get("source") == SOURCE_FIXED
                else f"{float(s['usage']):,.{s['usage_dp']}f} {s['usage_unit']}",
                fmt_money(s["subtotal"]),
            )
            for s in statement["supplies"]
        ]
        pay_rows = []
        if statement.get("due"):
            pay_rows.append(("Pay by", fmt_day(date.fromisoformat(statement["due"]))))
        if pay.get("account_name"):
            pay_rows.append(("Account name", pay["account_name"]))
        if pay.get("sort_code"):
            pay_rows.append(("Sort code", pay["sort_code"]))
        if pay.get("account_number"):
            pay_rows.append(("Account number", pay["account_number"]))
        pay_rows.append(("Reference", statement["reference"]))

        greeting = f"Hello {people['tenant_name']}," if people.get("tenant_name") else "Hello,"
        text = "\n".join(
            [
                greeting,
                "",
                f"Please find attached the {title.lower()} for {address}, covering {period}.",
                "",
                *[f"{n}: {u}  {a}" for n, u, a in rows],
                f"Total to pay: {total}",
                "",
                "How to pay:",
                *[f"{k}: {v}" for k, v in pay_rows],
                *(["", pay["note"]] if pay.get("note") else []),
                "",
                "This statement is generated automatically from the property's meter readings. "
                "Reply to this email with any questions.",
            ]
        )
        td = 'style="padding:4px 12px 4px 0"'
        tdr = 'style="padding:4px 0;text-align:right"'
        esc = html_lib.escape
        rows_h = [(esc(n), esc(u), esc(a)) for n, u, a in rows]
        pay_h = [(esc(k), esc(str(v))) for k, v in pay_rows]
        html = f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1f1e1d;max-width:560px">
<p>{esc(greeting)}</p>
<p>Please find attached the {esc(title.lower())} for {esc(address)}, covering {esc(period)}.</p>
<table style="border-collapse:collapse;width:100%;margin:12px 0">
{''.join(f'<tr><td {td}>{n}</td><td {td}>{u}</td><td {tdr}>{a}</td></tr>' for n, u, a in rows_h)}
<tr><td {td}><b>Total to pay</b></td><td {td}></td><td {tdr}><b>{total}</b></td></tr>
</table>
<p style="margin:16px 0 4px"><b>How to pay</b></p>
<table style="border-collapse:collapse">
{''.join(f'<tr><td {td}>{k}</td><td {td}>{v}</td></tr>' for k, v in pay_h)}
</table>
{f'<p>{esc(pay["note"])}</p>' if pay.get("note") else ''}
<p style="color:#6e6c66;font-size:12px;margin-top:20px">This statement is generated automatically from the property's meter readings. Reply to this email with any questions.</p>
</div>"""
        return subject, text, html

    # ------------------------------------------------------------------ live sensors
    async def _async_update_data(self) -> dict[str, Any]:
        today = dt_util.now().date()
        start, _ = self.period_for(today)
        start_dt = dt_util.start_of_local_day(start)
        now = dt_util.now()
        days = days_between(start, today + timedelta(days=1))
        out: dict[str, Any] = {"supplies": {}, "total_cost": None, "period_start": start.isoformat()}
        total = Decimal(0)
        any_cost = False
        for sid, sup in self.supplies.items():
            if sup.get("source", SOURCE_SENSOR) != SOURCE_SENSOR:
                continue
            try:
                usage, _ = await self._sensor_usage(sid, sup, start_dt, now, "preview")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Live usage failed for %s: %s", sid, err)
                usage = None
            if usage is None:
                out["supplies"][sid] = {"usage": None, "cost": None}
                continue
            priced = compute_charges(sup, usage["daily"], days)
            out["supplies"][sid] = {"usage": round(usage["usage"], 2), "cost": float(priced["subtotal"])}
            total += priced["subtotal"]
            any_cost = True
        out["total_cost"] = float(total) if any_cost else None
        return out


def _pdf_anchor(link: str, text: str) -> str:
    """Link that opens in a new tab. HA's front end swallows clicks on plain
    same-site links (it treats them as pages inside HA), so they'd do nothing."""
    return f'<a href="{html_lib.escape(link, quote=True)}" target="_blank">{text}</a>'


def _when(moment: datetime) -> str:
    local = dt_util.as_local(moment)
    return f"{fmt_day(local.date())} {local:%H:%M}"


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _units(supply: dict) -> tuple[str, str, int, int]:
    """(usage unit, reading unit, reading decimals, usage decimals)."""
    kind = supply["kind"]
    if supply.get("source") == SOURCE_FIXED:
        return "", "", 0, 0
    if kind == KIND_ELECTRICITY:
        return "kWh", "kWh", 1, 1
    if kind == KIND_GAS:
        if supply.get("unit") == UNIT_KWH:
            return "kWh", "kWh", 1, 1
        if supply.get("unit") == UNIT_FT3 and supply.get("source") == SOURCE_MANUAL:
            return "m³", "hundred ft³", 2, 2
        return "m³", "m³", 2, 2
    return "m³", "m³", 2, 2
