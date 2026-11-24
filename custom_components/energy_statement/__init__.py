"""Energy Statement: monthly utility statements from Home Assistant meters."""

from __future__ import annotations

import os
import re

import voluptuous as vol
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PDF_URL_BASE
from .manager import StatementManager

PLATFORMS = [Platform.SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.DATE]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_ENTRY = "config_entry_id"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.()\- ]+\.pdf$")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(StatementPdfView(hass))

    def _manager(call: ServiceCall) -> StatementManager:
        manager = hass.data[DOMAIN].get(call.data[ATTR_ENTRY])
        if manager is None:
            raise HomeAssistantError("That Energy Statement entry isn't loaded.")
        return manager

    async def create_statement(call: ServiceCall) -> None:
        await _manager(call).async_custom(call.data["start_date"], call.data["end_date"])

    async def preview_statement(call: ServiceCall) -> None:
        await _manager(call).async_preview()

    async def add_reading(call: ServiceCall) -> None:
        await _manager(call).async_add_reading(
            call.data["supply"], float(call.data["reading"]), call.data.get("reading_date")
        )

    async def approve(call: ServiceCall) -> None:
        await _manager(call).async_approve()

    async def discard(call: ServiceCall) -> None:
        await _manager(call).async_discard()

    base = {vol.Required(ATTR_ENTRY): cv.string}
    hass.services.async_register(
        DOMAIN, "create_statement", create_statement,
        schema=vol.Schema({**base, vol.Required("start_date"): cv.date, vol.Required("end_date"): cv.date}),
    )
    hass.services.async_register(DOMAIN, "preview_statement", preview_statement, schema=vol.Schema(base))
    hass.services.async_register(DOMAIN, "approve_statement", approve, schema=vol.Schema(base))
    hass.services.async_register(DOMAIN, "discard_statement", discard, schema=vol.Schema(base))
    hass.services.async_register(
        DOMAIN, "add_meter_reading", add_reading,
        schema=vol.Schema(
            {
                **base,
                vol.Required("supply"): vol.In(["electricity", "gas", "water"]),
                vol.Required("reading"): vol.Coerce(float),
                vol.Optional("reading_date"): cv.date,
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = StatementManager(hass, entry)
    await manager.async_setup()
    hass.data[DOMAIN][entry.entry_id] = manager
    await manager.coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))

    async def _started(_hass: HomeAssistant) -> None:
        await manager.async_check()

    entry.async_on_unload(async_at_started(hass, _started))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        manager: StatementManager = hass.data[DOMAIN].pop(entry.entry_id)
        await manager.async_unload()
    return ok


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


class StatementPdfView(HomeAssistantView):
    """Serve statement PDFs to logged-in users and signed links only."""

    url = PDF_URL_BASE + "/{entry_id}/{filename}"
    name = "api:energy_statement:pdf"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str, filename: str) -> web.StreamResponse:
        manager = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if manager is None or not SAFE_NAME.match(filename) or "/" in filename or ".." in filename:
            return web.Response(status=404)
        path = manager.pdf_path(filename)
        if not await self.hass.async_add_executor_job(os.path.isfile, path):
            return web.Response(status=404)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
