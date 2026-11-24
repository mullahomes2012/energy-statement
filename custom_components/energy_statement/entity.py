"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, VERSION
from .manager import StatementManager


class StatementEntity(CoordinatorEntity):
    """Entity attached to the property's device."""

    _attr_has_entity_name = True

    def __init__(self, manager: StatementManager, key: str, name: str, icon: str | None = None) -> None:
        super().__init__(manager.coordinator)
        self.manager = manager
        entry = manager.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        if icon:
            self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Energy Statement",
            model="Utility statements",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )
