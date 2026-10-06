"""The services that act on datasources: refresh one (async_update_gtfs),
shrink the picked ones, or every one (prune and intern).

A call names its sources by device, entity or plain name
(_wanted_files); each source is rewritten on a copy swapped in, under the
source's lock, so the sensors keep reading meanwhile and a refresh under
way is never undone (_rewrite_source). Registered by __init__.setup.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, DEFAULT_PATH, CONF_API_KEY, CONF_EXTRACT_FROM, CONF_URL
from .db_build import on_a_copy
from .shrink import async_train_routes, intern_gtfs_datasource, prune_gtfs_datasource
from .feed.files import real_path, routes_in
from .key_mask import note_key
from .feed.source_entries import async_ensure_datasource_entry, datasource_entry, source_readers, static_key_fields
from .source_refresh import (
    async_refresh_source, async_refresh_source_data, refresh_data_for, source_lock,
    source_zip_url,
)

_LOGGER = logging.getLogger(__name__)


def _wanted_files(hass: HomeAssistant, raw: str | list[str] | None) -> list[str]:
    """The datasource names a service call designates.

    The field is a device picker in the UI, so it usually carries the
    sources' device ids; yaml calls and old automations keep passing plain
    datasource names, or one bare string, and an entity id resolves too.
    A mix of the three still works.
    """
    if isinstance(raw, str):
        raw = [raw] if raw else []
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    files: list[str] = []
    for item in raw or []:
        entry_ids: list[str] = []
        if device := devices.async_get(item):
            entry_ids = list(device.config_entries)
        elif (entity := entities.async_get(item)) and entity.config_entry_id:
            entry_ids = [entity.config_entry_id]
        for entry_id in entry_ids:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry and entry.domain == DOMAIN and entry.data.get("file"):
                files.append(entry.data["file"])
                break
        else:
            # not a device nor an entity of ours: a plain datasource name
            files.append(item)
    return files


def _known_sources(hass: HomeAssistant) -> set[str]:
    """Every datasource name the integration knows.

    The datasource entries are the authoritative list - the bootstrap gives
    one to every source on disk, sensors or not - and the journey entries
    still count as a safety net for the short pre-bootstrap window.
    """
    return {e.data["file"] for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get("file")}


def _service_targets(hass: HomeAssistant,
                     data: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """The known sources a service call names, sorted, every known one when
    it names none; and the names it gave that are no known source."""
    wanted = _wanted_files(hass, data.get("file"))
    known = _known_sources(hass)
    if not wanted:
        return sorted(known), []
    unknown = sorted(set(wanted) - known)
    if unknown:
        _LOGGER.error("Unknown datasource(s): %s", ", ".join(unknown))
    return sorted(set(wanted) & known), unknown


async def _rewrite_source(hass: HomeAssistant, gtfs_dir: str, filename: str, dry_run: bool,
                          work: Callable[..., dict[str, Any] | None],
                          *args: object) -> tuple[dict[str, Any] | None, str | None]:
    """(what work returned, None), or (None, "refresh_running") when a
    refresh holds the source.

    A dry run only counts, nothing is written. Otherwise the work runs on a
    copy swapped in, so the sensors keep reading meanwhile, and under the
    source's lock: a refresh rebuilding the source would swap its own file
    in, and the work done on the one it replaces would be lost with it.
    """
    if dry_run:
        return await hass.async_add_executor_job(work, gtfs_dir, filename, *args, True), None
    lock = source_lock(hass, filename)
    if lock.locked():
        return None, "refresh_running"
    async with lock:
        return await hass.async_add_executor_job(
            on_a_copy, gtfs_dir, filename, work, *args, False), None


async def async_prune_datasources(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, list]:
    """Prune the picked datasources, or every one, down to the routes in use.

    A source nothing reads, or one a train or local stops sensor needs whole,
    is never attempted: it lands in the skipped list with its reason instead
    of an error per source, so sweeping every known source stays quiet.
    """
    dry_run = data.get("dry_run", False)
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    targets, unknown = _service_targets(hass, data)
    pruned: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for filename in targets:
        routes, unrestricted = source_readers(hass, filename)
        if not unrestricted:
            routes |= await async_train_routes(hass, gtfs_dir, filename)
        if unrestricted:
            _LOGGER.warning(
                "Skipping datasource %s: a train or local stops sensor reads "
                "the whole feed, pruning would remove data it needs", filename)
            skipped.append({"file": filename, "reason": "whole_feed_in_use"})
            continue
        if not routes:
            # no sensor reads it: pruning would empty the datasource
            skipped.append({"file": filename, "reason": "no_sensor_reads_it"})
            continue
        stats, busy = await _rewrite_source(hass, gtfs_dir, filename, dry_run,
                                            prune_gtfs_datasource, routes)
        if busy:
            skipped.append({"file": filename, "reason": busy})
            continue
        if stats:
            pruned.append(stats)
    result: dict[str, list] = {"pruned": pruned, "skipped": skipped}
    if unknown:
        result["unknown"] = unknown
    return result


async def async_intern_datasources(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, list]:
    """Intern the identifiers of the picked datasources, or every one.

    Same field contract as async_prune_datasources; interning has no route
    semantics, so every known source qualifies, sensors or not.
    """
    dry_run = data.get("dry_run", False)
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    targets, unknown = _service_targets(hass, data)
    interned: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for filename in targets:
        stats, busy = await _rewrite_source(hass, gtfs_dir, filename, dry_run,
                                            intern_gtfs_datasource)
        if busy:
            skipped.append({"file": filename, "reason": busy})
            continue
        if stats:
            interned.append(stats)
    result: dict[str, list] = {"interned": interned}
    if skipped:
        result["skipped"] = skipped
    if unknown:
        result["unknown"] = unknown
    return result


async def async_update_gtfs(hass: HomeAssistant, call_data: Mapping[str, Any]) -> bool:
    """The update_gtfs service.

    Refreshes the datasource through the scratch database: the fresh
    feed is filtered down to the routes actually followed, rebuilt
    beside the live file and swapped in, so the sensors never read a
    half-built database. A datasource with no line yet, a source this
    call creates included, is built whole the same way.

    A source that exists is refreshed from what it knows about itself,
    exactly like the update entity and the scheduled check: its own url,
    http(s) or file alike, and its key, and the call sets the per-import
    flags. An address the call gives that is not the source's own is
    fetched from this once, with the key the call gives and no other:
    the zip already in the folder, file://, while the host is down. The
    source keeps its own address for the next refreshes. A source that
    does not exist yet is created from the call's address and key, and
    keeps them; without an address, it is the zip of that name in the
    gtfs2 folder, by its file:// url.
    """
    note_key(call_data.get(CONF_API_KEY))
    _LOGGER.debug("Updating GTFS with: %s", call_data)
    data = dict(call_data)
    file = data.get("file", "")
    entry = datasource_entry(hass, file)
    if entry is not None:
        stored = refresh_data_for(hass, entry)
        url = (data.get(CONF_URL) or "").strip()
        key = (data.get(CONF_API_KEY) or "").strip()
        fetch = None
        if url and url != (stored.get(CONF_URL) or ""):
            # the source's key never goes to another address
            fetch = {CONF_URL: url, **static_key_fields(data)}
            _LOGGER.info("update_gtfs: %s fetched from the address given, this "
                         "once; the source keeps its own", file)
        elif key and key != (stored.get(CONF_API_KEY) or ""):
            _LOGGER.warning(
                "update_gtfs: the api_key given for %s differs from the "
                "source's own, which applies; change it on the "
                "source's configuration screen", file)
        return await async_refresh_source(
            hass, entry, fetch=fetch,
            flags={k: data[k] for k in ("clean_feed_info", "check_source_dates")
                   if k in data})
    # a source to create, from the address the call gives, else from the
    # zip of its name in the folder; fetched from its url either way
    data[CONF_URL] = (data.get(CONF_URL) or "").strip() or source_zip_url(hass, file)
    data[CONF_EXTRACT_FROM] = "url"
    ok = await async_refresh_source_data(hass, file, data)
    if ok:
        # the source is born with the address and key it was created
        # from, so the next refresh needs nothing but its name
        await async_ensure_datasource_entry(hass, file, url=data[CONF_URL], api=data)
    return ok


async def async_prune_line(hass: HomeAssistant, filename: str, route: str) -> str | None:
    """Drop one line's timetable from a datasource, every other line kept:
    the fix of a line no sensor reads any more. None once done (or when
    the line is already gone), else why it was not: a sensor reads the
    source whole or this line again, a refresh holds the source, or it is
    the source's last line, which is the source to remove instead."""
    routes, unrestricted = source_readers(hass, filename)
    if unrestricted:
        return "whole_feed_in_use"
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    if route in routes or route in await async_train_routes(hass, gtfs_dir, filename):
        return "line_read_again"
    present = await hass.async_add_executor_job(routes_in, real_path(gtfs_dir, filename))
    if not present or route not in present:
        return None
    keep = set(present) - {route}
    if not keep:
        return "last_line"
    _stats, busy = await _rewrite_source(hass, gtfs_dir, filename, False,
                                         prune_gtfs_datasource, keep)
    return busy
