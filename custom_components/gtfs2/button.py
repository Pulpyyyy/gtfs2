"""The button that rebuilds a source's database from its feed, now."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_KIND,
    ENTRY_KIND_DATASOURCE,
    CONF_FILE,
)
from .source_refresh import async_refresh_source, rebuild_pending, source_lock


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    ) -> None:
    """Only the datasource entries carry the button."""
    if config_entry.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE:
        return
    async_add_entities([GTFSSourceRefreshButton(hass, config_entry)])


class GTFSSourceRefreshButton(ButtonEntity):
    """Refresh one source whatever its versions say.

    The update entity installs only a version it knows to be new, and Home
    Assistant refuses the install when installed and latest agree, which
    is always the case with the checks off. This runs the same swap
    rebuild, sensors served throughout, for a feed republished under the
    same validators, a database to rebuild after a manual edit, or a source
    that never checks.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_name = "Refresh static feed"
    _attr_icon = "mdi:database-refresh"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._file = entry.data.get(CONF_FILE)
        self._attr_unique_id = f"gtfs2_source_refresh_{self._file}"
        # same device as the update entity and the switch, so the source
        # reads as one
        self._attr_device_info = DeviceInfo(
            name=f"GTFS - {self._file}",
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, f"GTFS datasource - {self._file}")},
            manufacturer="GTFS",
            model=self._file,
        )

    async def async_press(self) -> None:
        if source_lock(self.hass, self._file).locked():
            raise HomeAssistantError(f"A refresh of {self._file} is already running")
        # the update entity's rule: a zip already ahead of the database is
        # the feed, rebuilt from rather than downloaded again
        use_zip = await self.hass.async_add_executor_job(
            rebuild_pending, self.hass, self._file)
        if not await async_refresh_source(self.hass, self._entry, use_zip=use_zip):
            raise HomeAssistantError(
                f"The refresh of {self._file} failed, the current data stays")
