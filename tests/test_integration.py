"""End-to-end tests against a real Home Assistant core and recorder."""

from datetime import date, datetime, timedelta
from unittest.mock import patch
import os

import pytest
from homeassistant import config_entries
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.energy_statement.const import DOMAIN, STATUS_APPROVAL, STATUS_WAITING

SENSOR = "sensor.shelly_em1_total_energy"

ELEC = {
    "kind": "electricity", "source": "sensor", "name": "Electricity", "sensor": SENSOR,
    "charge_standing": True,
    "prices": [
        {"from": "2026-01-01", "unit_rate": 27.5, "standing": 53.0},
        {"from": "2026-10-15", "unit_rate": 26.0, "standing": 53.0},
    ],
}
BASE_OPTS = {
    "supplies": {"electricity": ELEC},
    "schedule": {"cycle": "monthly", "start_day": 1, "start_weekday": 0, "send_time": "09:00:00", "sending_mode": "approve"},
    "people": {"address": "Flat 2, 48 Example Road, London N16", "reference_prefix": "FLAT2",
               "landlord_name": "Mr A. Landlord", "landlord_email": "landlord@example.com",
               "tenant_name": "Ms B. Tenant", "tenant_email": "tenant@example.com"},
    "payment": {"pay_within_days": 14, "account_name": "A Landlord", "sort_code": "00-00-00", "account_number": "00000000"},
    "email": {"smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_security": "starttls",
              "smtp_username": "statements@example.com", "smtp_password": "x", "from_name": "Flat 2 statements",
              "reply_to": "landlord@example.com"},
}


@pytest.fixture(autouse=True)
async def london(hass):
    await hass.config.async_set_time_zone("Europe/London")


def local(*args) -> datetime:
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


async def _import_hourly(hass, start: datetime, hours: int, kwh_per_hour, opening: float):
    stats = []
    total = 0.0
    for h in range(hours):
        total += kwh_per_hour(h)
        stats.append({"start": dt_util.as_utc(start) + timedelta(hours=h), "state": opening + total, "sum": total})
    async_import_statistics(
        hass,
        {"mean_type": StatisticMeanType.NONE, "has_sum": True, "name": None, "source": "recorder",
         "statistic_id": SENSOR, "unit_class": "energy", "unit_of_measurement": "kWh"},
        stats,
    )
    await async_wait_recording_done(hass)
    return total


async def _setup(hass, opts, freezer, when):
    freezer.move_to(when)
    hass.states.async_set(SENSOR, "18600", {"state_class": "total_increasing", "unit_of_measurement": "kWh", "device_class": "energy"})
    entry = MockConfigEntry(domain=DOMAIN, title="Flat 2", data={}, options=opts)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


async def test_config_flow(recorder_mock, hass: HomeAssistant):
    hass.states.async_set(SENSOR, "18600", {"state_class": "total_increasing", "unit_of_measurement": "kWh", "device_class": "energy"})
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["step_id"] == "user"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"title": "Flat 2", "kind": "electricity", "source": "sensor"})
    assert r["step_id"] == "supply"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "name": "Electricity", "sensor": SENSOR, "unit_rate": 27.5, "charge_standing": True, "standing": 53, "effective_from": "2026-10-01"})
    assert r["step_id"] == "schedule", r.get("errors")
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "cycle": "monthly", "start_day": 1, "start_weekday": "0", "send_time": "09:00:00", "sending_mode": "approve"})
    assert r["step_id"] == "people"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**BASE_OPTS["people"], "tenant_email": "bad"})
    assert r["errors"] == {"tenant_email": "invalid_email"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], BASE_OPTS["people"])
    assert r["step_id"] == "payment"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"pay_within_days": 14, "account_name": "A", "sort_code": "123456", "account_number": "12345678"})
    assert r["step_id"] == "email", r.get("errors")
    with patch("custom_components.energy_statement.config_flow.test_connection") as tc:
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {k: v for k, v in BASE_OPTS["email"].items()})
        assert tc.called
    assert r["type"] == "create_entry", r
    opts = r["result"].options
    assert opts["payment"]["sort_code"] == "12-34-56"
    assert opts["supplies"]["electricity"]["prices"] == [{"from": "2026-10-01", "unit_rate": 27.5, "standing": 53.0}]
    await hass.async_block_till_done()

    # Options: add a manual water supply through the menu
    r = await hass.config_entries.options.async_init(r["result"].entry_id)
    assert r["type"] == "menu" and "add_supply" in r["menu_options"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "add_supply"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "water", "source": "manual"})
    assert r["step_id"] == "supply"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {
        "name": "Water", "clean_rate": 2.13, "waste_rate": 1.68, "waste_pct": 95, "charge_standing": False,
        "standing": 0, "effective_from": "2026-10-01", "initial_reading": 612.2, "initial_reading_date": "2026-09-28"})
    assert r["type"] == "create_entry", r
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert "water" in entry.options["supplies"]
    assert hass.states.get("number.flat_2_water_new_reading") is not None
    assert hass.states.get("button.flat_2_water_save_reading") is not None

    # Schedule: set "Billing starts on"
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "schedule"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {
        "cycle": "monthly", "start_day": 1, "start_weekday": "0", "send_time": "09:00:00",
        "sending_mode": "approve", "billing_start": "2026-11-21"})
    assert r["type"] == "create_entry", r
    await hass.async_block_till_done()
    m = hass.data[DOMAIN][entry.entry_id]
    assert m.data["last_period_end"] == "2026-11-21"


async def test_monthly_statement_with_approval(recorder_mock, hass: HomeAssistant, freezer):
    entry, m = await _setup(hass, BASE_OPTS, freezer, local(2026, 11, 1, 8, 0))
    # Oct has 31 days + 1 hour for the clocks going back (25 Oct) = 745 hours
    start = local(2026, 10, 1)
    hours = int((dt_util.as_utc(local(2026, 11, 1)) - dt_util.as_utc(start)).total_seconds() // 3600)
    assert hours == 745
    total = await _import_hourly(hass, start - timedelta(hours=1), hours + 1, lambda h: 0.0 if h == 0 else 0.55, 18204.3)
    m.data["last_period_end"] = "2026-10-01"

    # Before send time: nothing happens
    await m.async_check()
    assert m.data["pending"] is None

    freezer.move_to(local(2026, 11, 1, 9, 0, 5))
    await m.async_check()
    pending = m.data["pending"]
    assert pending and pending["status"] == STATUS_APPROVAL, pending
    st = pending["statement"]
    assert st["reference"] == "FLAT2-0001"
    s = st["supplies"][0]
    assert abs(float(s["usage"]) - 409.8) < 0.11, s["usage"]   # 745 h × 0.55
    assert s["open"] == pytest.approx(18204.3)
    assert s["close"] == pytest.approx(18204.3 + 0.55 * 745)
    texts = [l["text"] for l in s["lines"]]
    assert any("(1 to 14 Oct 2026)" in t for t in texts) and any("26.00p" in t for t in texts), texts
    assert os.path.exists(m.pdf_path(st["pdf"]))
    assert hass.states.get("sensor.flat_2_statement_status").state == STATUS_APPROVAL

    sent = {}
    def fake_send(cfg, to, cc, subject, text, html, attachment):
        sent.update(to=to, cc=cc, subject=subject, text=text, name=attachment[0], size=len(attachment[1]))
    with patch("custom_components.energy_statement.manager.send_mail", side_effect=fake_send):
        await hass.services.async_call("button", "press", {"entity_id": "button.flat_2_approve_and_send_statement"}, blocking=True)
        await hass.async_block_till_done()
    assert sent["to"] == ["tenant@example.com"] and sent["cc"] == ["landlord@example.com"]
    assert sent["name"] == "FLAT2-0001.pdf" and sent["size"] > 1000
    assert "Electricity usage statement" in sent["subject"]
    print(sent["subject"]); print(sent["text"])
    assert m.data["pending"] is None
    assert m.data["last"]["status"] == "sent"
    assert m.data["last_period_end"] == "2026-11-01"
    assert hass.states.get("sensor.flat_2_last_statement").state == st["total"]
    nxt = hass.states.get("sensor.flat_2_next_statement").state
    assert nxt.startswith("2026-12-01T09:00"), nxt


async def test_auto_send_and_live_sensors(recorder_mock, hass: HomeAssistant, freezer):
    opts = {**BASE_OPTS, "schedule": {**BASE_OPTS["schedule"], "sending_mode": "auto"}}
    entry, m = await _setup(hass, opts, freezer, local(2026, 11, 10, 12, 0))
    await _import_hourly(hass, local(2026, 10, 31, 23), 24 * 10 + 12, lambda h: 0.0 if h == 0 else 0.5, 18600)
    await m.coordinator.async_refresh()
    await hass.async_block_till_done()
    usage = float(hass.states.get("sensor.flat_2_electricity_usage_this_period").state)
    assert usage == pytest.approx(114.0), usage  # 228 h x 0.5
    cost = float(hass.states.get("sensor.flat_2_electricity_cost_this_period").state)
    assert cost > 30

    # Preview is never emailed
    with patch("custom_components.energy_statement.manager.send_mail") as sm:
        link = await m.async_preview()
        assert "authSig=" in link and not sm.called

    # Custom statement, auto mode -> sent straight away
    with patch("custom_components.energy_statement.manager.send_mail") as sm:
        await m.async_custom(date(2026, 11, 1), date(2026, 11, 5))
        assert sm.called
    assert m.data["last"]["type"] == "custom" and m.data["last"]["status"] == "sent"
    assert m.data["last_period_end"] == "2026-11-01"  # custom doesn't move the schedule


async def test_manual_water_waits_for_reading(recorder_mock, hass: HomeAssistant, freezer):
    water = {"kind": "water", "source": "manual", "name": "Water", "charge_standing": False,
             "prices": [{"from": "2026-01-01", "clean_rate": 2.13, "waste_rate": 1.68, "waste_pct": 95}],
             "initial_reading": {"t": local(2026, 9, 29, 12).isoformat(), "v": 611.0}}
    opts = {**BASE_OPTS, "supplies": {"electricity": ELEC, "water": water}}
    entry, m = await _setup(hass, opts, freezer, local(2026, 10, 20, 12, 0))
    await _import_hourly(hass, local(2026, 9, 30, 23), 24 * 33, lambda h: 0.0 if h == 0 else 0.5, 18000)
    m.data["last_period_end"] = "2026-10-01"

    # Reading taken mid-October
    await m.async_add_reading("water", 616.0)
    with pytest.raises(Exception):
        await m.async_add_reading("water", 600.0)  # lower than previous

    freezer.move_to(local(2026, 11, 1, 9, 0, 5))
    await m.async_check()
    assert m.data["pending"]["status"] == STATUS_WAITING
    assert "Water" in m.data["pending"]["missing"][0]
    assert hass.states.get("sensor.flat_2_statement_status").state == STATUS_WAITING

    # Reading arrives on 3 Nov through the phone notification
    freezer.move_to(local(2026, 11, 3, 12, 0))
    hass.bus.async_fire("mobile_app_notification_action",
                        {"action": f"ESTMT_READING_{entry.entry_id}_water", "reply_text": "622.4"})
    await hass.async_block_till_done()
    await m._lock.acquire(); m._lock.release()
    await hass.async_block_till_done()
    pending = m.data["pending"]
    assert pending["status"] == STATUS_APPROVAL, pending
    st = pending["statement"]
    w = next(s for s in st["supplies"] if s["kind"] == "water")
    # Pro-rated: 611.0 @ 29 Sep 12:00 -> 616.0 @ 20 Oct 12:00 -> 622.4 @ 3 Nov 12:00
    u = dt_util.as_utc  # real elapsed time, including the 25 Oct clock change
    t1, t2, t3 = u(local(2026, 9, 29, 12)), u(local(2026, 10, 20, 12)), u(local(2026, 11, 3, 12))
    v_open = 611.0 + 5.0 * (u(local(2026, 10, 1)) - t1) / (t2 - t1)
    v_close = 616.0 + 6.4 * (u(local(2026, 11, 1)) - t2) / (t3 - t2)
    assert w["open"] == pytest.approx(v_open) and w["close"] == pytest.approx(v_close)
    assert "pro-rated" in w["open_label"]
    print([l["text"] for l in w["lines"]], st["total"])
    assert st["supplies"][0]["kind"] == "electricity"

    with patch("custom_components.energy_statement.manager.send_mail"):
        await m.async_approve()
    # Next period starts exactly where this one was billed to
    assert m.data["billed_to"]["water"]["date"] == "2026-11-01"
    assert m.data["billed_to"]["water"]["value"] == pytest.approx(v_close)


async def _import_rows(hass, rows):
    async_import_statistics(
        hass,
        {"mean_type": StatisticMeanType.NONE, "has_sum": True, "name": None, "source": "recorder",
         "statistic_id": SENSOR, "unit_class": "energy", "unit_of_measurement": "kWh"},
        rows,
    )
    await async_wait_recording_done(hass)


async def test_v11_billing_start_offline_fixed_water_manual_gas(recorder_mock, hass: HomeAssistant, freezer):
    """The real Chardmore set-up: tenancy from 21 Nov, Shelly offline over month end."""
    gas = {"kind": "gas", "source": "manual", "name": "Gas", "unit": "m3", "charge_standing": True,
           "prices": [{"from": "2026-01-01", "unit_rate": 6.24, "standing": 31.65, "cf": 1.02264, "cv": 39.5}],
           "initial_reading": {"t": local(2026, 11, 20, 12).isoformat(), "v": 4120.0}}
    water = {"kind": "water", "source": "fixed", "name": "Water", "charge_standing": False,
             "prices": [{"from": "2026-01-01", "monthly": 30.0}]}
    opts = {**BASE_OPTS,
            "supplies": {"electricity": {**ELEC, "prices": [{"from": "2026-01-01", "unit_rate": 27.5, "standing": 53.0}]},
                         "gas": gas, "water": water},
            "schedule": {**BASE_OPTS["schedule"], "billing_start": "2026-11-21"}}
    entry, m = await _setup(hass, opts, freezer, local(2026, 11, 24, 18, 0))
    assert m.data["last_period_end"] == "2026-11-21"
    assert hass.states.get("sensor.flat_2_next_statement").state.startswith("2026-12-01T09:00")
    assert hass.states.get("sensor.flat_2_water_usage_this_period") is None  # fixed: no meter sensors

    # Shelly: 0.5 kWh every hour. Online until 29 Nov 12:00, offline until 2 Dec 11:00.
    hour = timedelta(hours=1)
    t0 = dt_util.as_utc(local(2026, 11, 20))
    off, back = dt_util.as_utc(local(2026, 11, 29, 12)), dt_util.as_utc(local(2026, 12, 2, 10))
    def rows(until):
        out, t, total = [], t0, 0.0
        while t < until:
            total += 0.5
            if not (off <= t < back):
                out.append({"start": t, "state": 1500 + total, "sum": total})
            t += hour
        return out
    await _import_rows(hass, rows(dt_util.as_utc(local(2026, 12, 1, 9))))

    freezer.move_to(local(2026, 12, 1, 9, 0, 5))
    await m.async_check()
    pending = m.data["pending"]
    assert pending["status"] == STATUS_WAITING
    assert any("Electricity" in x and "offline since 29 Nov 2026 12:00" in x for x in pending["missing"]), pending
    assert any(x.startswith("Gas:") for x in pending["missing"])

    # 2 Dec: WiFi fixed, Shelly back; gas reading entered
    freezer.move_to(local(2026, 12, 2, 12, 0))
    await _import_rows(hass, rows(dt_util.as_utc(local(2026, 12, 2, 11))))
    await m.async_add_reading("gas", 4160.0)
    await hass.async_block_till_done()
    await m._lock.acquire(); m._lock.release()
    await hass.async_block_till_done()
    pending = m.data["pending"]
    assert pending["status"] == STATUS_APPROVAL, pending
    st = pending["statement"]
    assert (st["start"], st["end"]) == ("2026-11-21", "2026-11-30")
    sup = {s["id"]: s for s in st["supplies"]}

    e = sup["electricity"]
    assert float(e["usage"]) == pytest.approx(120.0)          # 240 h × 0.5, gap split pro-rata
    assert any("offline from 29 Nov 2026 12:00" in n for n in e["notes"]), e["notes"]
    assert "pro-rated" in e["close_label"]
    texts = [l["text"] for l in e["lines"]]
    assert "Standing charge: 10 days × 53.00p" in texts, texts

    w = sup["water"]
    assert w["lines"][0]["text"] == "Fixed charge, 21 to 30 Nov 2026: £30.00 a month × 10 of 30 days"
    assert w["subtotal"] == "10.00"

    g = sup["gas"]
    assert g["open"] == pytest.approx(4120 + 40 * 12 / 288)
    assert g["close"] == pytest.approx(4120 + 40 * 252 / 288)
    print("\n".join(l["text"] + "  " + l["amount"] for s in st["supplies"] for l in s["lines"]), "TOTAL", st["total"])

    with patch("custom_components.energy_statement.manager.send_mail"):
        await m.async_approve()
    assert m.data["last_period_end"] == "2026-12-01"

    # December: the 18 kWh already billed in November is not billed again
    freezer.move_to(local(2027, 1, 2, 12, 0))
    await _import_rows(hass, rows(dt_util.as_utc(local(2027, 1, 1, 6))))
    await m.async_add_reading("gas", 4300.0)
    st2, missing, _ = await m._build(date(2026, 12, 1), date(2027, 1, 1), "regular")
    assert not missing, missing
    sup2 = {s["id"]: s for s in st2["supplies"]}
    assert float(sup2["electricity"]["usage"]) == pytest.approx(372.0)  # 31 days × 24 h × 0.5
    assert sup2["water"]["lines"][0] == {"text": "Fixed charge, December 2026", "amount": "30.00"}
    assert sup2["gas"]["open"] == pytest.approx(g["close"])  # continues exactly where November ended
    assert sup2["electricity"]["open"] == pytest.approx(e["close"])
    daily = dict(sup2["electricity"]["daily"])
    assert daily["2026-12-01"] == pytest.approx(12.0) and daily["2026-12-05"] == pytest.approx(12.0), daily

    from custom_components.energy_statement.pdf import render_pdf
    open("/tmp/v11_statement.pdf", "wb").write(render_pdf(st, m.people, m.payment))
