"""The switch that silences a source's realtime without losing its config."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_KIND,
    ENTRY_KIND_DATASOURCE,
    CONF_FILE,
    CONF_RT_ENABLED,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    ) -> None:
    """Only the datasource entries carry the switch."""
    if config_entry.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE:
        return
    async_add_entities([GTFSDatasourceRTSwitch(hass, config_entry)])


class GTFSDatasourceRTSwitch(SwitchEntity):
    """Realtime on or off for one source, its configuration kept.

    For a provider outage, a test or a debug session: switching off stops
    every fetch of the source's feeds within a cycle, and switching back on
    finds the urls and the key exactly where they were - nothing to retype,
    unlike emptying the fields. The state lives in the datasource entry's
    options, so it survives restarts, and flipping it runs through the same
    update the realtime screens use, mirror to the journey entries included.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_name = "Realtime enabled"
    _attr_icon = "mdi:rss"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._file = entry.data.get(CONF_FILE)
        self._attr_unique_id = f"gtfs2_datasource_rt_enabled_{self._file}"
        # same device as the realtime diagnostic, so the source reads as one
        self._attr_device_info = DeviceInfo(
            name=f"GTFS - {self._file}",
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, f"GTFS datasource - {self._file}")},
            manufacturer="GTFS",
            model=self._file,
        )

    @property
    def is_on(self) -> bool:
        return self._entry.options.get(CONF_RT_ENABLED, True)

    async def async_turn_on(self, **kwargs) -> None:
        self._set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._set_enabled(False)

    def _set_enabled(self, value: bool) -> None:
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_RT_ENABLED: value})
        self.async_write_ha_state()
