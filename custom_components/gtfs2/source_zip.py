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

from . import zip_file as zipfile
from .const import (CONF_API_KEY, CONF_API_KEY_LOCATION, CONF_API_KEY_NAME,
                    CONF_INNER_ZIP)
from .direction_repair import repair_trip_directions
from .freshness import adopt_zip, stage_zip
from .gtfs_db import import_routes, optimise_datasource, real_path, routes_in, swap_in
from .key_mask import fetch
from .rt_source import with_query_key
from .zip_peek import (extract_member, inner_zips, inner_zips_in_file,
                       member_out_of, open_member)
from .gtfs_filter import filter_gtfs_zip, read_zip_routes, zip_only_future_dates
from .gtfs_helper import check_extracting, get_gtfs, remove_from_zip

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
    url = with_query_key(url, data)
    if key and data.get(CONF_API_KEY_LOCATION) == "header":
        headers[(data.get(CONF_API_KEY_NAME) or "api_key")] = key
    return url, headers


def _open_source(data, url, headers):
    """The response whose body is this source's feed.

    The url's own body, or the member of it the source was built from: one
    call that both the first download and every refresh go through, so a
    source that named a network keeps getting that network. A host that
    stops answering ranges falls back to the whole envelope, which
    member_out_of then thins down to the same member.
    """
    inner = data.get(CONF_INNER_ZIP)
    if inner:
        member = open_member(url, headers, inner)
        if member is not None:
            return member
        _LOGGER.info("Fetching the whole envelope to take %s out of it", inner)
    return fetch("get", url, headers=headers, allow_redirects=True, timeout=15,
                 stream=True)


def _offer_or_take(data, zip_path):
    """Handle a zip on disk that holds zips: offer its networks, or take one.

    Returns an error code for the flow, or None when the file is usable as
    it is. The user's own file is left where it is until a network is
    picked, so a wrong pick is one screen back and not one download again.
    """
    inner = inner_zips_in_file(zip_path)
    if not inner:
        return None
    chosen = data.get(CONF_INNER_ZIP)
    if chosen not in inner:
        # nothing picked yet, or an edition that dropped the network this
        # source followed: either way the list is what the user needs
        data["inner_zips"] = inner
        return "zip_holds_zips"
    staged = zip_path + ".new"
    if not extract_member(zip_path, chosen, staged):
        return "no_data_file"
    os.replace(staged, zip_path)
    _LOGGER.info("Kept %s as the feed of %s", chosen, zip_path)
    return None


def _holds_a_feed(zip_path):
    """None when the zip is a GTFS feed, else why it cannot be one.

    Some publishers answer a perfectly valid zip that holds other zips:
    SEPTA's gtfs_public.zip carries google_bus.zip and google_rail.zip, one
    per network. Everything downstream then reads a feed without routes and
    the route screen ends on "no routes with trips", which says the lines
    carry no timetable when the truth is that no lines were ever read.
    """
    try:
        with zipfile.ZipFile(zip_path) as zin:
            members = {name.rsplit("/", 1)[-1] for name in zin.namelist()}
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Could not read the source zip %s: %s", zip_path, ex)
        return "no_zip_file"
    if "routes.txt" in members:
        return None
    inner = sorted(name for name in members if name.endswith(".zip"))
    if inner:
        _LOGGER.error("%s holds zips, not a feed: %s", zip_path, ", ".join(inner))
        return "zip_holds_zips"
    _LOGGER.error("No routes.txt in %s: %s", zip_path, ", ".join(sorted(members)[:6]))
    return "no_data_file"


def ensure_source_zip(hass, path, data):
    """Make sure the source zip is in place, without starting any import.

    The front half of get_gtfs: same checks, same download, same error codes,
    minus the part that unpacks the feed into a database. The config flow
    calls this when a source is submitted, so the lines can be chosen from
    the zip alone and the import can wait until it knows which routes to
    keep - on a national feed, the difference between a flow that continues
    and one that ends on a progress notification.

    Returns None when the zip is ready, else the code the flow already
    words: "extracting", "no_zip_file", "no_data_file", "zip_holds_zips".
    """
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    filename = data["file"]
    zip_path = os.path.join(gtfs_dir, filename + ".zip")
    if check_extracting(hass, path, filename):
        return "extracting"
    if data["extract_from"] == "zip":
        if not os.path.exists(zip_path):
            return "no_zip_file"
        return _offer_or_take(data, zip_path) or _holds_a_feed(zip_path)
    if not os.path.exists(zip_path):
        try:
            url, headers = _source_request(data)
            # what the url answers is read before it is fetched: an envelope
            # of zips holds one network per member, and the user picks which
            # before a byte of the wrong one is downloaded
            if not data.get(CONF_INNER_ZIP):
                inner = inner_zips(url, headers)
                if inner:
                    data["inner_zips"] = inner
                    return "zip_holds_zips"
            r = _open_source(data, url, headers)
            r.raise_for_status()
            staged = stage_zip(r, zip_path, data.get(CONF_INNER_ZIP),
                               envelope_ok=not data.get(CONF_INNER_ZIP))
            if staged is None:
                return "no_data_file"
            adopt_zip(r, staged, zip_path)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("The given URL or GTFS data file/folder was not found: %s", ex)
            return "no_data_file"
    # a host that refuses ranges answered the envelope whole: the networks
    # are offered from the file, and the pick taken out of it
    return _offer_or_take(data, zip_path) or _holds_a_feed(zip_path)


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


def _refresh_whole_feed(gtfs_dir, filename, zip_name, zip_path, data):
    """Rebuild a source some sensor reads whole: every line of the new edition.

    A train or a local stops sensor matches across the whole feed, so its
    source holds every line. The route by route refresh took the lines to
    keep from the database being replaced, and a line the new edition
    brought never came in, at any refresh. Here the new edition decides:
    all its lines go through the filter, which keeps the zip on disk
    untouched where the unfiltered import strips it of its shapes, and the
    import is the new database itself, no copy between two. Swapped in the
    same way as the route by route one.
    """
    routes = sorted({row["route_id"] for row in read_zip_routes(zip_path)})
    if not routes:
        _LOGGER.error("Refresh of %s aborted, the new edition names no line, "
                      "the current data stays", filename)
        return False
    real = real_path(gtfs_dir, filename)
    staging = filename + ".refresh"
    new_real = real_path(gtfs_dir, staging)
    try:
        if os.path.exists(new_real):
            os.remove(new_real)
        if not build_scratch_database(gtfs_dir, zip_name, new_real,
                                      data.get("clean_feed_info", False),
                                      only_routes=routes):
            _LOGGER.error("Refresh of %s aborted, the new edition could not be "
                          "imported, the current data stays", filename)
            return False
        loaded = routes_in(new_real)
        if not loaded:
            _LOGGER.error("Refresh of %s aborted, the new database holds no "
                          "trip, the current data stays", filename)
            return False
        # a route sensor may share the source with the whole-feed readers:
        # its line must still run in the new edition, as the route by route
        # refresh requires, or the swap leaves that sensor empty unsaid
        missing = set(data.get("read_routes") or ()) - loaded
        if missing:
            _LOGGER.error("Refresh of %s aborted, the new edition has no trip "
                          "for %s, the current data stays", filename, sorted(missing))
            data["lines_missing"] = sorted(missing)
            return False
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
    _LOGGER.info("Refreshed datasource %s from its source, whole: %s lines",
                 filename, len(loaded))
    # the same answer as the route by route refresh, for refresh_source
    return {route: None for route in sorted(loaded)}


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
            r = _open_source(data, url, headers)
            r.raise_for_status()
            # a host that stopped answering ranges sends the whole envelope:
            # the network picked is taken out of it here
            staged = stage_zip(r, zip_path, data.get(CONF_INNER_ZIP))
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

    if data.get("whole_feed"):
        return _refresh_whole_feed(gtfs_dir, filename, zip_name, zip_path, data)

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
            # every line gone says the file is broken, so every line is named;
            # the caller, back on the loop, tells the user
            data["lines_missing"] = sorted(gone if len(gone) == len(routes) else read)
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
