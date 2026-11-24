"""Number inputs for manual meter readings."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, KIND_NAMES
from .entity import StatementEntity
from .manager import StatementManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    m: StatementManager = hass.data[DOMAIN][entry.entry_id]
    add(
        ReadingInput(m, sid, sup.get("name") or KIND_NAMES[sup["kind"]])
        for sid, sup in m.manual_supplies().items()
    )


class ReadingInput(StatementEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 100_000_000
    _attr_native_step = 0.001

    def __init__(self, manager: StatementManager, sid: str, name: str) -> None:
        super().__init__(manager, f"{sid}_new_reading", f"{name} new reading", "mdi:counter")
        self.sid = sid

    @property
    def native_value(self) -> float | None:
        return self.manager.inputs.get(f"reading_{self.sid}")

    async def async_set_native_value(self, value: float) -> None:
        self.manager.inputs[f"reading_{self.sid}"] = value
        self.async_write_ha_state()
