"""Setup and Configure screens for Energy Statement."""

from __future__ import annotations

import copy
import re
from datetime import date, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector as sel
from homeassistant.util import dt as dt_util

from .const import (
    CYCLE_MONTHLY,
    CYCLE_QUARTERLY,
    CYCLE_WEEKLY,
    DEFAULT_GAS_CORRECTION,
    DEFAULT_GAS_CV,
    DOMAIN,
    KIND_ELECTRICITY,
    KIND_GAS,
    KIND_NAMES,
    KIND_WATER,
    KINDS,
    MODE_APPROVE,
    MODE_AUTO,
    OPT_EMAIL,
    OPT_PAYMENT,
    OPT_PEOPLE,
    OPT_SCHEDULE,
    OPT_SUPPLIES,
    SECURITY_NONE,
    SECURITY_SSL,
    SECURITY_STARTTLS,
    SOURCE_FIXED,
    SOURCE_MANUAL,
    SOURCE_SENSOR,
    UNIT_FT3,
    UNIT_KWH,
    UNIT_M3,
)
from .mailer import MailError, send_mail, test_connection

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ENERGY_UNITS = {"Wh", "kWh", "MWh", "GWh", "MJ", "GJ"}
VOLUME_UNITS = {"m³", "ft³", "CCF", "L", "gal", "mL"}

PRICE_KEYS = {
    KIND_ELECTRICITY: ["unit_rate", "standing"],
    KIND_GAS: ["unit_rate", "standing", "cf", "cv"],
    KIND_WATER: ["clean_rate", "waste_rate", "waste_pct", "standing"],
}


def _opt(value: str, label: str) -> sel.SelectOptionDict:
    return sel.SelectOptionDict(value=value, label=label)


def _select(options: list[sel.SelectOptionDict], custom: bool = False) -> sel.SelectSelector:
    return sel.SelectSelector(
        sel.SelectSelectorConfig(options=options, mode=sel.SelectSelectorMode.DROPDOWN, custom_value=custom)
    )


def _num(min_: float, max_: float, step: float | str, unit: str | None = None) -> sel.NumberSelector:
    cfg = sel.NumberSelectorConfig(min=min_, max=max_, step=step, mode=sel.NumberSelectorMode.BOX)
    if unit:
        cfg["unit_of_measurement"] = unit
    return sel.NumberSelector(cfg)


TEXT = sel.TextSelector()
EMAIL = sel.TextSelector(sel.TextSelectorConfig(type=sel.TextSelectorType.EMAIL))
PASSWORD = sel.TextSelector(sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD))
MULTILINE = sel.TextSelector(sel.TextSelectorConfig(multiline=True))
BOOL = sel.BooleanSelector()
DATE = sel.DateSelector()


def _current_price(supply: dict | None) -> dict:
    if not supply or not supply.get("prices"):
        return {}
    today = dt_util.now().date().isoformat()
    prices = sorted(supply["prices"], key=lambda p: p["from"])
    current = prices[0]
    for p in prices:
        if p["from"] <= today:
            current = p
    return current


# ---------------------------------------------------------------- schemas
def supply_schema(kind: str, source: str, existing: dict | None, adding: bool) -> vol.Schema:
    price = _current_price(existing)
    ex = existing or {}
    today = dt_util.now().date().isoformat()
    fields: dict[Any, Any] = {
        vol.Required("name", default=ex.get("name", KIND_NAMES[kind])): TEXT,
    }
    if source == SOURCE_FIXED:
        fields[vol.Required("monthly", default=price.get("monthly", 0))] = _num(0, 100000, 0.01, "£/month")
        fields[vol.Required("effective_from", default=today)] = DATE
        return vol.Schema(fields)
    if source == SOURCE_SENSOR:
        device_class = {KIND_ELECTRICITY: ["energy"], KIND_GAS: ["gas", "energy"], KIND_WATER: ["water"]}[kind]
        fields[vol.Required("sensor", default=ex.get("sensor")) if ex.get("sensor") else vol.Required("sensor")] = (
            sel.EntitySelector(sel.EntitySelectorConfig(domain="sensor", device_class=device_class))
        )
    if kind == KIND_GAS:
        units = [_opt(UNIT_M3, "Cubic metres (m³)"), _opt(UNIT_KWH, "kWh (no conversion)")]
        if source == SOURCE_MANUAL:
            units.insert(1, _opt(UNIT_FT3, "Hundreds of cubic feet (imperial meter)"))
        fields[vol.Required("unit", default=ex.get("unit", UNIT_M3))] = _select(units)
    if kind in (KIND_ELECTRICITY, KIND_GAS):
        fields[vol.Required("unit_rate", default=price.get("unit_rate", 0))] = _num(0, 1000, 0.001, "p/kWh")
    if kind == KIND_GAS:
        fields[vol.Required("cf", default=price.get("cf", DEFAULT_GAS_CORRECTION))] = _num(0.9, 1.1, "any")
        fields[vol.Required("cv", default=price.get("cv", DEFAULT_GAS_CV))] = _num(30, 45, 0.1, "MJ/m³")
    if kind == KIND_WATER:
        fields[vol.Required("clean_rate", default=price.get("clean_rate", 0))] = _num(0, 100, "any", "£/m³")
        fields[vol.Required("waste_rate", default=price.get("waste_rate", 0))] = _num(0, 100, "any", "£/m³")
        fields[vol.Required("waste_pct", default=price.get("waste_pct", 100))] = _num(0, 100, 0.5, "%")
    fields[vol.Required("charge_standing", default=ex.get("charge_standing", False))] = BOOL
    fields[vol.Required("standing", default=price.get("standing", 0))] = _num(0, 10000, 0.001, "p/day")
    fields[vol.Required("effective_from", default=today)] = DATE
    if source == SOURCE_MANUAL and adding:
        fields[vol.Optional("initial_reading")] = _num(0, 100_000_000, 0.001)
        fields[vol.Optional("initial_reading_date", default=today)] = DATE
    return vol.Schema(fields)


def supply_from_input(kind: str, source: str, ui: dict, existing: dict | None) -> dict:
    supply = copy.deepcopy(existing) if existing else {"kind": kind, "source": source, "prices": []}
    supply["name"] = ui["name"].strip() or KIND_NAMES[kind]
    supply["charge_standing"] = bool(ui.get("charge_standing"))
    if "sensor" in ui:
        supply["sensor"] = ui["sensor"]
    if kind == KIND_GAS:
        supply["unit"] = ui.get("unit", UNIT_M3)
    keys = ["monthly"] if source == SOURCE_FIXED else PRICE_KEYS[kind]
    new_price = {k: float(ui[k]) for k in keys if k in ui}
    current = _current_price(existing)
    changed = not existing or any(float(current.get(k, -1)) != v for k, v in new_price.items())
    if changed:
        eff = ui.get("effective_from") or dt_util.now().date().isoformat()
        prices = [p for p in supply["prices"] if p["from"] != eff]
        prices.append({"from": eff, **new_price})
        supply["prices"] = sorted(prices, key=lambda p: p["from"])
    if not existing and source == SOURCE_MANUAL and ui.get("initial_reading") is not None:
        now = dt_util.now()
        day = date.fromisoformat(ui.get("initial_reading_date") or now.date().isoformat())
        when = now if day >= now.date() else dt_util.start_of_local_day(day) + timedelta(hours=12)
        supply["initial_reading"] = {"t": when.isoformat(), "v": float(ui["initial_reading"])}
    return supply


def validate_supply(hass: HomeAssistant, kind: str, source: str, ui: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    if source == SOURCE_SENSOR:
        state = hass.states.get(ui.get("sensor", ""))
        if state is None:
            errors["sensor"] = "sensor_missing"
        else:
            if state.attributes.get("state_class") not in ("total", "total_increasing"):
                errors["sensor"] = "not_metered"
            unit = state.attributes.get("unit_of_measurement")
            wants_energy = kind == KIND_ELECTRICITY or ui.get("unit") == UNIT_KWH
            if unit and wants_energy and unit not in ENERGY_UNITS:
                errors["sensor"] = "wrong_unit_energy"
            elif unit and not wants_energy and unit not in VOLUME_UNITS:
                errors["sensor"] = "wrong_unit_volume"
    return errors


def schedule_schema(hass: HomeAssistant, ex: dict) -> vol.Schema:
    notify = sorted(f"notify.{s}" for s in hass.services.async_services_for_domain("notify") if s not in ("notify", "send_message", "persistent_notification"))
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    fields: dict[Any, Any] = {
        vol.Required("cycle", default=ex.get("cycle", CYCLE_MONTHLY)): _select(
            [_opt(CYCLE_MONTHLY, "Monthly"), _opt(CYCLE_QUARTERLY, "Quarterly (Jan, Apr, Jul, Oct)"), _opt(CYCLE_WEEKLY, "Weekly")]
        ),
        vol.Required("start_day", default=ex.get("start_day", 1)): _num(1, 28, 1),
        vol.Required("start_weekday", default=str(ex.get("start_weekday", 0))): _select(
            [_opt(str(i), d) for i, d in enumerate(weekdays)]
        ),
        vol.Optional("billing_start", description={"suggested_value": ex.get("billing_start")}): DATE,
        vol.Required("send_time", default=ex.get("send_time", "09:00:00")): sel.TimeSelector(),
        vol.Required("sending_mode", default=ex.get("sending_mode", MODE_APPROVE)): _select(
            [_opt(MODE_APPROVE, "Ask me first (phone notification)"), _opt(MODE_AUTO, "Send automatically")]
        ),
        vol.Optional("notify_service", description={"suggested_value": ex.get("notify_service")}): _select(
            [_opt(n, n) for n in notify], custom=True
        ),
    }
    return vol.Schema(fields)


def clean_schedule(ui: dict) -> dict:
    out = dict(ui)
    out["start_day"] = int(out["start_day"])
    out["start_weekday"] = int(out["start_weekday"])
    for key in ("notify_service", "billing_start"):
        if not out.get(key):
            out.pop(key, None)
    return out


def people_schema(ex: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("address", default=ex.get("address", "")): MULTILINE,
            vol.Optional("reference_prefix", description={"suggested_value": ex.get("reference_prefix")}): TEXT,
            vol.Required("landlord_name", default=ex.get("landlord_name", "")): TEXT,
            vol.Required("landlord_email", default=ex.get("landlord_email", "")): EMAIL,
            vol.Required("tenant_name", default=ex.get("tenant_name", "")): TEXT,
            vol.Required("tenant_email", default=ex.get("tenant_email", "")): EMAIL,
        }
    )


def validate_people(ui: dict) -> dict[str, str]:
    errors = {}
    for key in ("landlord_email", "tenant_email"):
        if not EMAIL_RE.match(ui.get(key, "").strip()):
            errors[key] = "invalid_email"
    return errors


def payment_schema(ex: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("pay_within_days", default=ex.get("pay_within_days", 14)): _num(0, 90, 1, "days"),
            vol.Required("account_name", default=ex.get("account_name", "")): TEXT,
            vol.Required("sort_code", default=ex.get("sort_code", "")): TEXT,
            vol.Required("account_number", default=ex.get("account_number", "")): TEXT,
            vol.Optional("note", description={"suggested_value": ex.get("note")}): MULTILINE,
        }
    )


def validate_payment(ui: dict) -> tuple[dict[str, str], dict]:
    errors = {}
    out = dict(ui)
    out["pay_within_days"] = int(out["pay_within_days"])
    digits = re.sub(r"[\s-]", "", ui.get("sort_code", ""))
    if not re.fullmatch(r"\d{6}", digits):
        errors["sort_code"] = "invalid_sort_code"
    else:
        out["sort_code"] = f"{digits[:2]}-{digits[2:4]}-{digits[4:]}"
    acc = re.sub(r"\s", "", ui.get("account_number", ""))
    if not re.fullmatch(r"\d{8}", acc):
        errors["account_number"] = "invalid_account"
    else:
        out["account_number"] = acc
    return errors, out


def email_schema(ex: dict, landlord_email: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("smtp_host", default=ex.get("smtp_host", "")): TEXT,
            vol.Required("smtp_port", default=ex.get("smtp_port", 587)): _num(1, 65535, 1),
            vol.Required("smtp_security", default=ex.get("smtp_security", SECURITY_STARTTLS)): _select(
                [_opt(SECURITY_STARTTLS, "STARTTLS (usually port 587)"), _opt(SECURITY_SSL, "SSL/TLS (usually port 465)"), _opt(SECURITY_NONE, "None")]
            ),
            vol.Required("smtp_username", default=ex.get("smtp_username", "")): TEXT,
            vol.Required("smtp_password", default=ex.get("smtp_password", "")): PASSWORD,
            vol.Optional("from_email", description={"suggested_value": ex.get("from_email")}): EMAIL,
            vol.Required("from_name", default=ex.get("from_name", "Utility statements")): TEXT,
            vol.Optional("reply_to", description={"suggested_value": ex.get("reply_to", landlord_email)}): EMAIL,
            vol.Optional("test_email_to"): EMAIL,
        }
    )


async def validate_email(hass: HomeAssistant, ui: dict) -> tuple[dict[str, str], dict[str, str], dict]:
    errors: dict[str, str] = {}
    placeholders = {"smtp_error": ""}
    cfg = dict(ui)
    cfg["smtp_port"] = int(cfg["smtp_port"])
    test_to = (cfg.pop("test_email_to", "") or "").strip()
    for key in ("from_email", "reply_to"):
        if cfg.get(key) and not EMAIL_RE.match(cfg[key].strip()):
            errors[key] = "invalid_email"
    if test_to and not EMAIL_RE.match(test_to):
        errors["test_email_to"] = "invalid_email"
    if not cfg.get("from_email") and not EMAIL_RE.match(cfg.get("smtp_username", "")):
        errors["from_email"] = "from_needed"
    if errors:
        return errors, placeholders, cfg
    try:
        if test_to:
            await hass.async_add_executor_job(
                send_mail, cfg, [test_to], [], "Energy Statement test email",
                "This is a test from the Energy Statement integration in Home Assistant. Email sending works.",
                None, None,
            )
        else:
            await hass.async_add_executor_job(test_connection, cfg)
    except MailError as err:
        errors["base"] = "smtp_failed"
        placeholders["smtp_error"] = str(err)
    return errors, placeholders, cfg


SOURCE_OPTIONS = [
    _opt(SOURCE_SENSOR, "Home Assistant sensor (smart or pulse meter)"),
    _opt(SOURCE_MANUAL, "Manual readings"),
    _opt(SOURCE_FIXED, "Fixed monthly charge (no meter)"),
]


# ---------------------------------------------------------------- config flow
class EnergyStatementConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._opts: dict[str, Any] = {OPT_SUPPLIES: {}}
        self._kind = KIND_ELECTRICITY
        self._source = SOURCE_SENSOR

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnergyStatementOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._opts["title"] = user_input["title"].strip()
            self._kind = user_input["kind"]
            self._source = user_input["source"]
            return await self.async_step_supply()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("title", default="Flat electricity"): TEXT,
                    vol.Required("kind", default=KIND_ELECTRICITY): _select([_opt(k, KIND_NAMES[k]) for k in KINDS]),
                    vol.Required("source", default=SOURCE_SENSOR): _select(SOURCE_OPTIONS),
                }
            ),
        )

    async def async_step_supply(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_supply(self.hass, self._kind, self._source, user_input)
            if not errors:
                self._opts[OPT_SUPPLIES][self._kind] = supply_from_input(self._kind, self._source, user_input, None)
                return await self.async_step_schedule()
        return self.async_show_form(
            step_id="supply",
            data_schema=supply_schema(self._kind, self._source, None, True),
            errors=errors,
            description_placeholders={"supply": KIND_NAMES[self._kind]},
        )

    async def async_step_schedule(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._opts[OPT_SCHEDULE] = clean_schedule(user_input)
            return await self.async_step_people()
        return self.async_show_form(step_id="schedule", data_schema=schedule_schema(self.hass, {}))

    async def async_step_people(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_people(user_input)
            if not errors:
                self._opts[OPT_PEOPLE] = user_input
                return await self.async_step_payment()
        return self.async_show_form(
            step_id="people", data_schema=people_schema(user_input or {}), errors=errors
        )

    async def async_step_payment(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, cleaned = validate_payment(user_input)
            if not errors:
                self._opts[OPT_PAYMENT] = cleaned
                return await self.async_step_email()
        return self.async_show_form(
            step_id="payment", data_schema=payment_schema(user_input or {}), errors=errors
        )

    async def async_step_email(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"smtp_error": ""}
        landlord = self._opts[OPT_PEOPLE].get("landlord_email", "")
        if user_input is not None:
            errors, placeholders, cfg = await validate_email(self.hass, user_input)
            if not errors:
                self._opts[OPT_EMAIL] = cfg
                title = self._opts.pop("title")
                return self.async_create_entry(title=title, data={}, options=self._opts)
        return self.async_show_form(
            step_id="email",
            data_schema=email_schema(user_input or {}, landlord),
            errors=errors,
            description_placeholders=placeholders,
        )


# ---------------------------------------------------------------- options flow
class EnergyStatementOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._opts: dict[str, Any] | None = None
        self._sid: str | None = None
        self._kind: str | None = None
        self._source: str | None = None
        self._adding = False

    @property
    def opts(self) -> dict[str, Any]:
        if self._opts is None:
            self._opts = copy.deepcopy({**self.config_entry.data, **self.config_entry.options})
        return self._opts

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self.opts)

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        supplies = self.opts.get(OPT_SUPPLIES, {})
        menu = ["edit_supply"]
        if len(supplies) < len(KINDS):
            menu.append("add_supply")
        if len(supplies) > 1:
            menu.append("remove_supply")
        menu += ["schedule", "people", "payment", "email"]
        names = ", ".join(s.get("name", KIND_NAMES[s["kind"]]) for s in supplies.values())
        return self.async_show_menu(step_id="init", menu_options=menu, description_placeholders={"supplies": names})

    def _supply_choices(self) -> list[sel.SelectOptionDict]:
        return [_opt(sid, s.get("name", KIND_NAMES[s["kind"]])) for sid, s in self.opts[OPT_SUPPLIES].items()]

    async def async_step_edit_supply(self, user_input: dict | None = None) -> ConfigFlowResult:
        supplies = self.opts[OPT_SUPPLIES]
        if len(supplies) == 1:
            user_input = {"supply": next(iter(supplies))}
        if user_input is not None:
            self._sid = user_input["supply"]
            sup = supplies[self._sid]
            self._kind, self._source, self._adding = sup["kind"], sup["source"], False
            return await self.async_step_supply()
        return self.async_show_form(
            step_id="edit_supply",
            data_schema=vol.Schema({vol.Required("supply"): _select(self._supply_choices())}),
        )

    async def async_step_add_supply(self, user_input: dict | None = None) -> ConfigFlowResult:
        free = [k for k in KINDS if k not in self.opts[OPT_SUPPLIES]]
        if user_input is not None:
            self._kind = self._sid = user_input["kind"]
            self._source = user_input["source"]
            self._adding = True
            return await self.async_step_supply()
        return self.async_show_form(
            step_id="add_supply",
            data_schema=vol.Schema(
                {
                    vol.Required("kind", default=free[0]): _select([_opt(k, KIND_NAMES[k]) for k in free]),
                    vol.Required("source", default=SOURCE_MANUAL): _select(SOURCE_OPTIONS),
                }
            ),
        )

    async def async_step_supply(self, user_input: dict | None = None) -> ConfigFlowResult:
        assert self._kind and self._source and self._sid
        existing = None if self._adding else self.opts[OPT_SUPPLIES][self._sid]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_supply(self.hass, self._kind, self._source, user_input)
            if not errors:
                self.opts[OPT_SUPPLIES][self._sid] = supply_from_input(self._kind, self._source, user_input, existing)
                return self._save()
        return self.async_show_form(
            step_id="supply",
            data_schema=supply_schema(self._kind, self._source, existing, self._adding),
            errors=errors,
            description_placeholders={"supply": KIND_NAMES[self._kind]},
        )

    async def async_step_remove_supply(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm"):
                errors["confirm"] = "confirm_needed"
            else:
                self.opts[OPT_SUPPLIES].pop(user_input["supply"], None)
                return self._save()
        return self.async_show_form(
            step_id="remove_supply",
            data_schema=vol.Schema(
                {vol.Required("supply"): _select(self._supply_choices()), vol.Required("confirm", default=False): BOOL}
            ),
            errors=errors,
        )

    async def async_step_schedule(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.opts[OPT_SCHEDULE] = clean_schedule(user_input)
            return self._save()
        return self.async_show_form(
            step_id="schedule", data_schema=schedule_schema(self.hass, self.opts.get(OPT_SCHEDULE, {}))
        )

    async def async_step_people(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_people(user_input)
            if not errors:
                self.opts[OPT_PEOPLE] = user_input
                return self._save()
        return self.async_show_form(
            step_id="people", data_schema=people_schema(user_input or self.opts.get(OPT_PEOPLE, {})), errors=errors
        )

    async def async_step_payment(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, cleaned = validate_payment(user_input)
            if not errors:
                self.opts[OPT_PAYMENT] = cleaned
                return self._save()
        return self.async_show_form(
            step_id="payment", data_schema=payment_schema(user_input or self.opts.get(OPT_PAYMENT, {})), errors=errors
        )

    async def async_step_email(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"smtp_error": ""}
        if user_input is not None:
            errors, placeholders, cfg = await validate_email(self.hass, user_input)
            if not errors:
                self.opts[OPT_EMAIL] = cfg
                return self._save()
        return self.async_show_form(
            step_id="email",
            data_schema=email_schema(
                user_input or self.opts.get(OPT_EMAIL, {}), self.opts.get(OPT_PEOPLE, {}).get("landlord_email", "")
            ),
            errors=errors,
            description_placeholders=placeholders,
        )
