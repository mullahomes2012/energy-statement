"""Sensors: live usage and cost, statement status."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    KIND_ELECTRICITY,
    KIND_GAS,
    KIND_ICONS,
    KIND_NAMES,
    KIND_WATER,
    SOURCE_MANUAL,
    SOURCE_SENSOR,
    STATUS_APPROVAL,
    STATUS_IDLE,
    STATUS_WAITING,
    UNIT_FT3,
    UNIT_KWH,
)
from .entity import StatementEntity
from .manager import StatementManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    manager: StatementManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for sid, sup in manager.supplies.items():
        name = sup.get("name") or KIND_NAMES[sup["kind"]]
        if sup.get("source") == SOURCE_MANUAL:
            entities.append(LastReadingSensor(manager, sid, sup, name))
        elif sup.get("source", SOURCE_SENSOR) == SOURCE_SENSOR:
            entities.append(UsageSensor(manager, sid, sup, name))
            entities.append(CostSensor(manager, sid, name))
    if any(s.get("source", SOURCE_SENSOR) == SOURCE_SENSOR for s in manager.supplies.values()):
        entities.append(TotalCostSensor(manager))
    entities += [LastStatementSensor(manager), StatusSensor(manager), NextStatementSensor(manager)]
    add(entities)


class UsageSensor(StatementEntity, SensorEntity):
    def __init__(self, manager: StatementManager, sid: str, sup: dict, name: str) -> None:
        super().__init__(manager, f"{sid}_usage", f"{name} usage this period", KIND_ICONS[sup["kind"]])
        self.sid = sid
        kind = sup["kind"]
        if kind == KIND_ELECTRICITY or sup.get("unit") == UNIT_KWH:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = "kWh"
        else:
            self._attr_device_class = SensorDeviceClass.GAS if kind == KIND_GAS else SensorDeviceClass.WATER
            self._attr_native_unit_of_measurement = "m³"
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        return ((self.coordinator.data or {}).get("supplies", {}).get(self.sid) or {}).get("usage")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"period_start": (self.coordinator.data or {}).get("period_start")}


class CostSensor(StatementEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_suggested_display_precision = 2

    def __init__(self, manager: StatementManager, sid: str, name: str) -> None:
        super().__init__(manager, f"{sid}_cost", f"{name} cost this period", "mdi:cash")
        self.sid = sid

    @property
    def native_value(self) -> float | None:
        return ((self.coordinator.data or {}).get("supplies", {}).get(self.sid) or {}).get("cost")


class TotalCostSensor(StatementEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_suggested_display_precision = 2

    def __init__(self, manager: StatementManager) -> None:
        super().__init__(manager, "total_cost", "Total cost this period", "mdi:cash-multiple")

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("total_cost")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manual = [s.get("name") for s in self.manager.supplies.values() if s.get("source", SOURCE_SENSOR) != SOURCE_SENSOR]
        return {"not_included": manual} if manual else {}


class LastReadingSensor(StatementEntity, SensorEntity):
    _attr_suggested_display_precision = 2

    def __init__(self, manager: StatementManager, sid: str, sup: dict, name: str) -> None:
        super().__init__(manager, f"{sid}_last_reading", f"{name} last reading", "mdi:counter")
        self.sid = sid
        kind = sup["kind"]
        if sup.get("unit") == UNIT_KWH:
            self._attr_native_unit_of_measurement = "kWh"
        elif sup.get("unit") == UNIT_FT3:
            self._attr_native_unit_of_measurement = "ft³ (hundreds)"
        elif kind in (KIND_GAS, KIND_WATER):
            self._attr_native_unit_of_measurement = "m³"
        else:
            self._attr_native_unit_of_measurement = "kWh"

    @property
    def native_value(self) -> float | None:
        last = self.manager.last_reading(self.sid)
        return last[1] if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.manager.last_reading(self.sid)
        readings = self.manager.data["readings"].get(self.sid, [])
        return {
            "reading_taken": last[0].isoformat() if last else None,
            "recent_readings": [f"{r['t'][:16].replace('T', ' ')}: {r['v']}" for r in readings[-6:]],
        }


class LastStatementSensor(StatementEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_suggested_display_precision = 2

    def __init__(self, manager: StatementManager) -> None:
        super().__init__(manager, "last_statement", "Last statement", "mdi:file-document-outline")

    @property
    def native_value(self) -> float | None:
        last = self.manager.data.get("last")
        return float(last["total"]) if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.manager.data.get("last") or {}
        attrs = {k: last.get(k) for k in ("reference", "start", "end", "issued", "due", "status", "type", "at")}
        attrs["supplies"] = last.get("supplies")
        if last.get("pdf"):
            attrs["pdf_path"] = f"{DOMAIN}/{self.manager.entry.entry_id}/{last['pdf']}"
        return attrs


class StatusSensor(StatementEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATUS_IDLE, STATUS_WAITING, STATUS_APPROVAL]

    def __init__(self, manager: StatementManager) -> None:
        super().__init__(manager, "status", "Statement status", "mdi:email-check-outline")
        self._attr_translation_key = "status"

    @property
    def native_value(self) -> str:
        return self.manager.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pending = self.manager.data.get("pending") or {}
        attrs: dict[str, Any] = {}
        if pending.get("missing"):
            attrs["waiting_for"] = pending["missing"]
        if pending.get("statement"):
            st = pending["statement"]
            attrs.update(reference=st["reference"], total=st["total"], start=st["start"], end=st["end"])
            if st.get("error"):
                attrs["send_error"] = st["error"]
        elif pending:
            attrs.update(start=pending.get("start"))
        return attrs


class NextStatementSensor(StatementEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager: StatementManager) -> None:
        super().__init__(manager, "next_statement", "Next statement", "mdi:calendar-clock")

    @property
    def native_value(self) -> datetime | None:
        return self.manager.next_statement_at()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        start = self.manager.data.get("last_period_end")
        if not start:
            return {}
        s = date.fromisoformat(start)
        return {"covers_from": s.isoformat(), "covers_to_exclusive": self.manager.period_for(s)[1].isoformat()}
