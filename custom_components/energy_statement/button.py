"""Buttons: approve, discard, preview, custom statement, readings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, KIND_NAMES
from .entity import StatementEntity
from .manager import StatementManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    m: StatementManager = hass.data[DOMAIN][entry.entry_id]

    async def custom() -> None:
        await m.async_custom(m.inputs["custom_start"], m.inputs["custom_end"])

    entities = [
        ActionButton(m, "approve", "Approve and send statement", "mdi:send-check", m.async_approve),
        ActionButton(m, "handed_over", "Mark statement as handed over", "mdi:hand-extended-outline", m.async_mark_handed_over),
        ActionButton(m, "discard", "Discard pending statement", "mdi:file-cancel-outline", m.async_discard),
        ActionButton(m, "preview", "Preview this period", "mdi:file-eye-outline", m.async_preview),
        ActionButton(m, "custom", "Create custom statement", "mdi:calendar-range", custom),
    ]
    for sid, sup in m.manual_supplies().items():
        name = sup.get("name") or KIND_NAMES[sup["kind"]]

        async def save(sid: str = sid, name: str = name) -> None:
            value = m.inputs.get(f"reading_{sid}")
            if value is None:
                raise HomeAssistantError(f"Enter the {name.lower()} reading first.")
            await m.async_add_reading(sid, float(value), m.inputs.get(f"reading_date_{sid}"))

        async def delete(sid: str = sid) -> None:
            await m.async_delete_last_reading(sid)

        entities.append(ActionButton(m, f"{sid}_save_reading", f"{name} save reading", "mdi:content-save", save))
        entities.append(
            ActionButton(m, f"{sid}_delete_reading", f"{name} delete last reading", "mdi:delete-outline", delete)
        )
    add(entities)


class ActionButton(StatementEntity, ButtonEntity):
    def __init__(
        self, manager: StatementManager, key: str, name: str, icon: str, action: Callable[[], Awaitable]
    ) -> None:
        super().__init__(manager, key, name, icon)
        self._action = action

    async def async_press(self) -> None:
        await self._action()
