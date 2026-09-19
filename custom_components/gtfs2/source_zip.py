"""The source zip beside a datasource: fetched, kept, refreshed.

The zip is the only complete record of a feed, so the integration keeps it
next to the database it built from it. ensure_source_zip fetches it when a
flow starts from a url, open_datasource reads a database without touching
it, build_scratch_database unpacks a filtered copy into a scratch file, and
refresh_datasource fetches a new edition and swaps it in. The staging and
adopting of a download live in freshness, the import itself in gtfs_db.
Called from the config flow, from __init__ (the update service) and from
source_refresh.
"""
from __future__ import annotations

import gc
import logging
import os

import pygtfs
import requests

from .const import CONF_API_KEY, CONF_API_KEY_LOCATION, CONF_API_KEY_NAME
from .direction_repair import repair_trip_directions
from .freshness import adopt_zip, stage_zip
from .gtfs_db import import_routes, optimise_datasource, real_path, routes_in, swap_in
from .gtfs_filter import filter_gtfs_zip, zip_only_future_dates
from .gtfs_helper import check_extracting, get_gtfs, remove_from_zip
from .notifications import async_notify_lines_missing

_LOGGER = logging.getLogger(__name__)


def build_scratch_database(gtfs_dir, file, scratch_file, clean_feed_info=False,
                           only_routes=None):
    """Unpack a zip into the scratch database, synchronously.

    The counterpart of extract_from_zip, minus the fork: the caller is already
    off the event loop, and an import has to be finished before its routes can
    be copied out. Nothing here touches the real database.

    only_routes cuts the feed down to those routes before pygtfs sees it.
    pygtfs pays per row, so this is what makes a national feed usable:
    measured on gtfs-nl.zip, 15.1 M stop_times filtered in 40 s down to a
    feed pygtfs imports in half a second, where the full import built a
    2.6 GB scratch file. When the filter cannot run, the whole feed is
    imported as before: only slower, never wrong. The filtered path also
    leaves the source zip untouched, where the historic path strips tables
    out of it in place.

    Returns True when the scratch file holds a feed.
    """
    feed_file = os.path.join(gtfs_dir, file)
    filtered = None
    if only_routes:
        candidate = scratch_file + ".zip"
        if filter_gtfs_zip(feed_file, candidate, only_routes,
                           drop_feed_info=clean_feed_info) is not None:
            filtered = candidate
            feed_file = candidate
        else:
            _LOGGER.warning(
                "Could not filter %s, importing the whole feed instead", file)
    if filtered is None:
        drop = ['shapes.txt', 'transfers.txt', 'fare_attributes.txt',
                'levels.txt', 'pathways.txt', 'translations.txt']
        if clean_feed_info:
            drop.append('feed_info.txt')
        remove_from_zip(drop, gtfs_dir, file[:-4])

    # same connection arguments as the real database, so the scratch one
    # behaves identically under a timeout
    conn = f"{scratch_file}?check_same_thread=False&timeout=60"
    try:
        scratch = pygtfs.Schedule(conn)
        pygtfs.append_feed(scratch, feed_file)
        ok = bool(scratch.feeds)
        if ok:
            # routes are copied out of this file into the real database, so
            # directions have to be right before the copy, not after
            repair_trip_directions(scratch)
        # the session holds a connection the pool does not know about, so
        # closing only the engine leaves the file open until garbage
        # collection - long enough for the cleanup below to fail on Windows
        scratch.session.close()
        scratch.engine.dispose()
        del scratch
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Could not unpack %s into the import database: %s", file, ex)
        return False
    finally:
        if filtered and os.path.exists(filtered):
            # pygtfs read the filtered zip through a handle it only drops on
            # collection, and Windows refuses to delete a file still open
            gc.collect()
            try:
                os.remove(filtered)
            except OSError as ex:
                _LOGGER.warning("Could not remove %s: %s", filtered, ex)
    if not ok:
        _LOGGER.error("The import database holds no feed after unpacking %s", file)
    return ok


def _source_request(data):
    """The url and headers a source's zip is fetched with, api key included."""
    url = data["url"]
    headers = {"User-Agent": "home-assistant-gtfs2"}
    key = data.get(CONF_API_KEY)
    if key and data.get(CONF_API_KEY_LOCATION) == "query_string":
        url = url + "?" + (data.get(CONF_API_KEY_NAME) or "api_key") + "=" + key
    if key and data.get(CONF_API_KEY_LOCATION) == "header":
        headers[(data.get(CONF_API_KEY_NAME) or "api_key")] = key
    return url, headers


def ensure_source_zip(hass, path, data):
    """Make sure the source zip is in place, without starting any import.

    The front half of get_gtfs: same checks, same download, same error codes,
    minus the part that unpacks the feed into a database. The config flow
    calls this when a source is submitted, so the lines can be chosen from
    the zip alone and the import can wait until it knows which routes to
    keep - on a national feed, the difference between a flow that continues
    and one that ends on a progress notification.

    Returns None when the zip is ready, else the code the flow already
    words: "extracting", "no_zip_file", "no_data_file".
    """
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    filename = data["file"]
    zip_path = os.path.join(gtfs_dir, filename + ".zip")
    if check_extracting(hass, path, filename):
        return "extracting"
    if data["extract_from"] == "zip":
        return None if os.path.exists(zip_path) else "no_zip_file"
    if not os.path.exists(zip_path):
        try:
            url, headers = _source_request(data)
            r = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
            r.raise_for_status()
            staged = stage_zip(r, zip_path)
            if staged is None:
                return "no_data_file"
            adopt_zip(r, staged, zip_path)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("The given URL or GTFS data file/folder was not found: %s", ex)
            return "no_data_file"
    return None


def open_datasource(gtfs_dir, filename):
    """Open a datasource that is known to exist, with no extracting gate.

    get_gtfs refuses to answer while anything writes to the file, because a
    journal used to mean the legacy fork was still building it in place. In
    the two database model the real file receives short legitimate writes
    while the sensors live - an index being added, an intern - so a journal
    can exist for milliseconds and means nothing. Callers that just proved
    the datasource exists, like the step after a finished import, open it
    here instead of walking into that gate.

    Returns the schedule, or None when the file is not there.
    """
    sqlite_file = os.path.join(gtfs_dir, filename + ".sqlite")
    if not os.path.exists(sqlite_file):
        _LOGGER.error("No datasource to open: %s", sqlite_file)
        return None
    return pygtfs.Schedule(f"{sqlite_file}?check_same_thread=False&timeout=60")


def refresh_datasource(hass, path, data):
    """Refresh a datasource from its source, keeping the sensors served.

    The legacy update rebuilt the real database in place: on a large feed
    the sensors read a half-built file for as long as the import took. Here
    the fresh feed is filtered down to the routes the database actually
    follows, unpacked into the scratch database, copied into a new real
    file built beside the old one, and the two are swapped in one rename.
    The coordinators reopen the file on their next cycle, so they only ever
    see the old complete data or the new complete data.

    Falls back to the legacy full extract when there is nothing to refresh
    from: no database yet, or one that follows no route.

    data may carry read_routes, the lines the source's sensors name: the
    new edition must still carry trips for those, or the swap is refused.

    Returns {route_id: stop_times} on success, False on failure, and
    whatever get_gtfs returns when it falls back.
    """
    gtfs_dir = hass.config.path(path)
    filename = data["file"]
    real = real_path(gtfs_dir, filename)
    loaded = routes_in(real)
    if loaded is None:
        # the file is there but would not answer: something holds it, a
        # VACUUM or an intern, or it is momentarily unreadable. Reading that
        # as "follows no route" would send this refresh down the legacy
        # path, which deletes the database and the zip and rebuilds the
        # whole network in place
        _LOGGER.error("Cannot read the routes of %s, keeping its data", filename)
        return False
    routes = sorted(loaded)
    if not routes:
        _LOGGER.info("Datasource %s follows no route yet, extracting it whole",
                     filename)
        return get_gtfs(hass, path, data, True)

    zip_name = filename + ".zip"
    zip_path = os.path.join(gtfs_dir, zip_name)
    if data.get("extract_from", "url") == "url":
        # download beside the current zip and swap only once complete and
        # proven to be a zip: the zip is the only full record of the feed
        # and must survive a failed or hijacked download
        try:
            url, headers = _source_request(data)
            r = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
            r.raise_for_status()
            staged = stage_zip(r, zip_path)
            if staged is None:
                return False
            adopt_zip(r, staged, zip_path)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Could not download %s: %s", data.get("url"), ex)
            fresh = zip_path + ".new"
            if os.path.exists(fresh):
                try:
                    os.remove(fresh)
                except OSError:
                    pass
            return False
    if not os.path.exists(zip_path):
        _LOGGER.error("No source zip to refresh %s from", filename)
        return False
    if data.get("check_source_dates", False) and zip_only_future_dates(zip_path):
        _LOGGER.info("New file contains only dates in the future, "
                     "keeping the current data")
        return False

    # the new real database is built under its own datasource name, so every
    # existing helper works on it unchanged and nothing it does can touch the
    # file the sensors are reading
    staging = filename + ".refresh"
    new_real = real_path(gtfs_dir, staging)

    def _build(scratch_file):
        return build_scratch_database(
            gtfs_dir, zip_name, scratch_file,
            data.get("clean_feed_info", False), only_routes=routes)

    try:
        if os.path.exists(new_real):
            os.remove(new_real)
        added = import_routes(gtfs_dir, staging, routes, _build)
        if added is None or len(added) < len(routes):
            _LOGGER.error("Refresh of %s aborted, the current data stays: %s",
                          filename, added)
            return False
        # a line the new edition carries no trip for: renumbered, retired,
        # or a broken feed. The copy of such a line succeeds with nothing
        # in it, so swapping would leave its sensors empty without a word.
        # The current data stays while a sensor still reads one, or when
        # nothing at all came through; a line nobody reads just goes.
        gone = {route for route, count in added.items() if not count}
        read = gone & set(data.get("read_routes") or ())
        if gone and (read or len(gone) == len(routes)):
            _LOGGER.error("Refresh of %s aborted, the new edition has no trip "
                          "for %s, the current data stays", filename, sorted(gone))
            # every line gone says the file is broken, so every line is named
            hass.create_task(async_notify_lines_missing(
                hass, filename, sorted(gone if len(gone) == len(routes) else read)))
            return False
        # intern only: everything in this file was just copied on purpose
        optimise_datasource(gtfs_dir, staging)
        if not swap_in(new_real, real):
            return False
    finally:
        for leftover in (new_real, new_real + "-journal"):
            if os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError as ex:
                    _LOGGER.warning("Could not remove %s: %s", leftover, ex)
    _LOGGER.info("Refreshed datasource %s from its source: %s stop_times "
                 "per route", filename, added)
    return added
