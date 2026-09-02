"""The update entity that says when a source's static feed has a new version.

One per datasource entry. Installed is what the database was last built
from, latest is what the last check learned from the host; the gap between
the two is "update available", and Install runs the swap refresh whatever
the source's mode is, so the entity doubles as a clean manual refresh
button even with the checks off. In notify mode this entity is the hook an
automation triggers on, and installs from, in whatever window suits the
install.
"""
from __future__ import annotations

import logging

import homeassistant.util.dt as dt_util
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    CONF_FILE,
    CONF_KIND,
    CONF_STATIC_REFRESH_MODE,
    ENTRY_KIND_DATASOURCE,
    STATIC_REFRESH_OFF,
)
from .source_refresh import (
    SIGNAL_SOURCE_REFRESH,
    async_refresh_source,
    check_interval,
    installed_meta,
    next_check_at,
    probe_state,
    source_lock,
    source_meta,
    version_label,
    _zip_path,
)

_LOGGER = logging.getLogger(__name__)


def _when(stamp):
    """An iso stamp as the local minute a human reads, or None."""
    moment = dt_util.parse_datetime(stamp) if stamp else None
    if not moment:
        return None
    return dt_util.as_local(moment).strftime("%Y-%m-%d %H:%M")


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    ) -> None:
    """Only the datasource entries carry the update entity."""
    if config_entry.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE:
        return
    entity = GTFSSourceUpdateEntity(hass, config_entry)
    await entity.async_load_versions()
    async_add_entities([entity])


class GTFSSourceUpdateEntity(UpdateEntity, RestoreEntity):
    """New versions of one source's static feed, and the button to install."""

    _attr_has_entity_name = True
    _attr_name = "Static feed"
    _attr_should_poll = False
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._file = entry.data.get(CONF_FILE)
        self._installed = None
        self._zip_version = None
        # the sidecars themselves, not just their labels: the attributes
        # answer from these, so a check costs the two reads it always did
        self._installed_meta = {}
        self._zip_meta = {}
        self._attr_unique_id = f"gtfs2_source_update_{self._file}"
        self._attr_title = f"GTFS static feed - {self._file}"
        # same device as the realtime diagnostic and switch, so the source
        # reads as one
        self._attr_device_info = DeviceInfo(
            name=f"GTFS - {self._file}",
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, f"GTFS datasource - {self._file}")},
            manufacturer="GTFS",
            model=self._file,
        )

    async def async_load_versions(self) -> None:
        """Re-read the sidecars; they are files, so never on the loop."""
        def _read():
            return (installed_meta(self.hass, self._file),
                    source_meta(_zip_path(self.hass, self._file)))
        self._installed_meta, self._zip_meta = (
            await self.hass.async_add_executor_job(_read))
        self._installed = version_label(self._installed_meta)
        self._zip_version = version_label(self._zip_meta)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # what the last check learned lives in memory (probe_state) and
        # would read unknown after every restart; the entity carries it in
        # its own attributes, so it re-seeds the store from its saved state
        # and a restart keeps the last verdict until a real check speaks.
        # The sidecars stay the durable record of what is installed.
        last = await self.async_get_last_state()
        if last is not None:
            probe = probe_state(self.hass, self._file)
            for probe_key, attr in (("latest", "latest_version"),
                                    ("checked_at", "last_check"),
                                    ("result", "last_check_result")):
                value = last.attributes.get(attr)
                if value and probe_key not in probe:
                    probe[probe_key] = value
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_SOURCE_REFRESH.format(self._file),
            self._async_source_moved))

    async def _async_source_moved(self) -> None:
        await self.async_load_versions()
        self.async_write_ha_state()

    @property
    def installed_version(self):
        return self._installed

    @property
    def latest_version(self):
        mode = self._entry.options.get(CONF_STATIC_REFRESH_MODE,
                                       STATIC_REFRESH_OFF)
        if mode == STATIC_REFRESH_OFF:
            # nothing checks, so nothing is claimed: the entity is a manual
            # refresh button that never pretends to know the host
            return self._installed
        latest = probe_state(self.hass, self._file).get("latest")
        if latest:
            return latest
        if self._zip_version and self._zip_version != self._installed:
            # the feed was fetched to know it was new; the zip is ahead of
            # the database until someone installs
            return self._zip_version
        # no check has spoken yet: claim the installed version, the same
        # stance mode off takes, rather than reading "unknown" until the
        # first night slot
        return self._installed

    @property
    def in_progress(self) -> bool:
        return source_lock(self.hass, self._file).locked()

    @property
    def release_summary(self):
        """What the database is, and whether that is known or assumed.

        installed_meta falls back on the zip's sidecar when no build was
        ever recorded, which asserts that database and zip are the same
        version. True of the flow's initial import, not of a download whose
        rebuild failed, so the dialog says which of the two it is looking at.
        """
        meta = self._installed_meta
        if not meta:
            return None
        built = _when(meta.get("built_at"))
        if built:
            summary = f"Database built {built}"
        else:
            summary = ("Database version assumed from the kept zip: no build "
                       "was ever recorded")
        downloaded = _when(meta.get("downloaded_at"))
        if downloaded:
            summary += f", from a feed downloaded {downloaded}"
        return summary[:255]

    @property
    def extra_state_attributes(self):
        state = probe_state(self.hass, self._file)
        meta = self._installed_meta
        built_at = meta.get("built_at")
        next_check = next_check_at(self.hass, self._entry,
                                   fallback_last=meta.get("downloaded_at"))
        return {
            "file": self._file,
            "last_check": state.get("checked_at"),
            "last_check_result": state.get("result"),
            "zip_version": self._zip_version,
            # what the database is made of, and how sure of it we are
            "built_at": built_at,
            "downloaded_at": meta.get("downloaded_at"),
            "version_source": "recorded" if built_at else "assumed",
            # the url the bytes actually came from, redirects followed
            "source_url": meta.get("url"),
            "source_size": meta.get("size"),
            # the schedule, so the entity says when it will look next
            "refresh_mode": self._entry.options.get(CONF_STATIC_REFRESH_MODE,
                                                    STATIC_REFRESH_OFF),
            "check_interval": check_interval(self._entry),
            "next_check": next_check,
        }

    async def async_install(self, version, backup: bool, **kwargs) -> None:
        """Rebuild the database from the source, sensors served throughout."""
        # when the zip is already ahead of the database, the feed is here:
        # rebuild from it instead of downloading the same bytes again
        use_zip = bool(self._zip_version
                       and self._zip_version != self._installed)
        ok = await async_refresh_source(self.hass, self._entry,
                                        use_zip=use_zip)
        if not ok:
            raise HomeAssistantError(
                f"The refresh of {self._file} failed, the current data stays")
        await self._async_source_moved()
