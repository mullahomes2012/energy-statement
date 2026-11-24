"""Date inputs: custom statement range and manual reading date."""

from __future__ import annotations

from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, KIND_NAMES
from .entity import StatementEntity
from .manager import StatementManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    m: StatementManager = hass.data[DOMAIN][entry.entry_id]
    entities = [
        InputDate(m, "custom_start", "Custom statement start", "mdi:calendar-start", restore=True),
        InputDate(m, "custom_end", "Custom statement end", "mdi:calendar-end", restore=True),
    ]
    for sid, sup in m.manual_supplies().items():
        name = sup.get("name") or KIND_NAMES[sup["kind"]]
        entities.append(InputDate(m, f"reading_date_{sid}", f"{name} reading date", "mdi:calendar"))
    add(entities)


class InputDate(StatementEntity, DateEntity, RestoreEntity):
    def __init__(self, manager: StatementManager, key: str, name: str, icon: str, restore: bool = False) -> None:
        super().__init__(manager, key, name, icon)
        self.key = key
        self._restore = restore

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._restore and (last := await self.async_get_last_state()) is not None:
            try:
                self.manager.inputs[self.key] = date.fromisoformat(last.state)
            except ValueError:
                pass

    @property
    def native_value(self) -> date | None:
        return self.manager.inputs.get(self.key)

    async def async_set_value(self, value: date) -> None:
        self.manager.inputs[self.key] = value
        self.async_write_ha_state()
