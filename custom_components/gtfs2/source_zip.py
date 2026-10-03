"""The source zip beside a datasource: fetched, kept, refreshed.

The zip is the only complete record of a feed, so the integration keeps it
next to the database it built from it. ensure_source_zip fetches it when a
flow starts from a url, build_scratch_database unpacks a filtered copy into
a scratch file, and refresh_datasource fetches a new edition and swaps it
in. The staging and adopting of a download live in freshness, the import
itself in db_build.
Called from the config flow, from __init__ (the update service) and from
source_refresh.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import gc
import logging
import os
from typing import TYPE_CHECKING, Any
import zipfile

import pygtfs

from .const import CONF_INNER_ZIP
from .direction_repair import repair_trip_directions
from .freshness import download_feed, keep_download, source_request
from .db_build import import_routes, optimise_datasource, swap_in
from .gtfs_db import feed_zip, real_path, remove_database, remove_files, routes_in, staging_name
from .zip_peek import extract_member, inner_zips, inner_zips_in_file
from .gtfs_filter import (feed_info_unreadable, filter_gtfs_zip, read_zip_routes,
                          zip_only_future_dates)
from .datasource import IMPORT_IGNORED, drop_import_indexes

if TYPE_CHECKING:
    # for the annotations only
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def build_scratch_database(gtfs_dir: str, file: str, scratch_file: str, clean_feed_info: bool = False,
                           only_routes: Iterable[str] | None = None) -> bool:
    """Unpack a zip into the scratch database, synchronously.

    The counterpart of extract_from_zip, minus the fork: the caller is already
    off the event loop, and an import has to be finished before its routes can
    be copied out. Nothing here touches the real database.

    only_routes cuts the feed down to those routes before pygtfs sees it.
    pygtfs pays per row, so this is what makes a national feed usable:
    measured on gtfs-nl.zip, 15.1 M stop_times filtered in 40 s down to a
    feed pygtfs imports in half a second, where the full import built a
    2.6 GB scratch file. When the filter cannot run, the whole feed is
    imported as before: only slower, never wrong. Neither path writes to
    the source zip: the filter writes its cut beside it, and the whole
    feed is read from it with the tables it leaves out skipped
    (IMPORT_IGNORED).

    Returns True when the scratch file holds a feed.
    """
    feed_file = os.path.join(gtfs_dir, file)
    if not clean_feed_info and feed_info_unreadable(feed_file):
        _LOGGER.warning("The feed_info.txt of %s has dates pygtfs cannot read, "
                        "importing without it", file)
        clean_feed_info = True
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
    ignored: tuple[str, ...] = ()
    if filtered is None:
        # the whole feed, read from the kept zip itself: pygtfs skips what
        # the filter would have left out, and the zip stays as it came
        ignored = IMPORT_IGNORED + (("feed_info.txt",) if clean_feed_info else ())

    # same connection arguments as the real database, so the scratch one
    # behaves identically under a timeout
    conn = f"{scratch_file}?check_same_thread=False&timeout=60"
    try:
        scratch = pygtfs.Schedule(conn)
        drop_import_indexes(scratch)
        pygtfs.append_feed(scratch, feed_file, ignore_files=ignored)
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
        _LOGGER.exception("Could not unpack %s into the import database: %s", file, ex)
        return False
    finally:
        if filtered and os.path.exists(filtered):
            # pygtfs read the filtered zip through a handle it only drops on
            # collection, and Windows refuses to delete a file still open
            gc.collect()
            remove_files(filtered)
    if not ok:
        _LOGGER.error("The import database holds no feed after unpacking %s", file)
    return ok


def _offer_or_take(data: dict[str, Any], zip_path: str) -> str | None:
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


def _holds_a_feed(zip_path: str) -> str | None:
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
    except (OSError, zipfile.BadZipFile) as ex:
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


def ensure_source_zip(hass: HomeAssistant, path: str, data: dict[str, Any]) -> str | None:
    """Make sure the source zip is in place, without starting any import.

    The front half of get_gtfs: same checks, same download, same error codes,
    minus the part that unpacks the feed into a database. The config flow
    calls this when a source is submitted, so the lines can be chosen from
    the zip alone and the import can wait until it knows which routes to
    keep - on a national feed, the difference between a flow that continues
    and one that ends on a progress notification.

    Returns None when the zip is ready, else the code the flow already
    words: "no_zip_file", "no_data_file", "zip_holds_zips". A source being
    created has no database for anything to be writing to.
    """
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    filename = data["file"]
    zip_path = feed_zip(gtfs_dir, filename)
    if data["extract_from"] == "zip":
        if not os.path.exists(zip_path):
            return "no_zip_file"
    elif not os.path.exists(zip_path):
        # what the url answers is read before it is fetched: an envelope
        # of zips holds one network per member, and the user picks which
        # before a byte of the wrong one is downloaded
        if not data.get(CONF_INNER_ZIP):
            inner = inner_zips(*source_request(data))
            if inner:
                data["inner_zips"] = inner
                return "zip_holds_zips"
        if not _fetch_zip(data, zip_path, envelope_ok=not data.get(CONF_INNER_ZIP)):
            return "no_data_file"
    # a host that refuses ranges answered the envelope whole: the networks
    # are offered from the file, and the pick taken out of it, as from a
    # zip the user put in the folder
    return _offer_or_take(data, zip_path) or _holds_a_feed(zip_path)


def _stale_staging_gone(new_real: str, filename: str) -> bool:
    """Clear the new database an earlier refresh left, before building one.

    Left by a crash or a restart mid-refresh. One that cannot go, held
    open on Windows or read-only, stops this refresh with the current data
    in place: raised, the error left the refresh without its notification.
    """
    if not os.path.exists(new_real):
        return True
    try:
        os.remove(new_real)
    except OSError as ex:
        _LOGGER.error("Refresh of %s aborted, %s left by an earlier refresh "
                      "cannot be removed, the current data stays: %s",
                      filename, new_real, ex)
        return False
    return True


def _refresh_whole_feed(gtfs_dir: str, filename: str, zip_name: str, zip_path: str,
                        data: dict[str, Any]) -> dict[str, None] | bool:
    """Rebuild a source some sensor reads whole: every line of the new edition.

    A train or a local stops sensor matches across the whole feed, so its
    source holds every line. The route by route refresh took the lines to
    keep from the database being replaced, and a line the new edition
    brought never came in, at any refresh. Here the new edition decides:
    all its lines go through the filter, which leaves out only the calendars
    no trip uses (measured on SNCF and Zou: the same stops, trips and stop
    times as the zip read whole, and no slower), and the import is the new
    database itself, no copy between two. Swapped in the same way as the
    route by route one.
    """
    routes = sorted({row["route_id"] for row in read_zip_routes(zip_path) if row["route_id"]})
    if not routes:
        _LOGGER.error("Refresh of %s aborted, the new edition names no line, "
                      "the current data stays", filename)
        return False
    real = real_path(gtfs_dir, filename)
    staging = staging_name(filename)
    new_real = real_path(gtfs_dir, staging)
    if not _stale_staging_gone(new_real, filename):
        return False
    try:
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
        # swapped in, the file is gone already; refused or failed, it goes
        # here, so the next refresh starts from nothing it left behind
        remove_database(new_real)
    _LOGGER.info("Refreshed datasource %s from its source, whole: %s lines",
                 filename, len(loaded))
    # the same answer as the route by route refresh, for refresh_source
    return {route: None for route in sorted(loaded)}


def _fetch_zip(data: Mapping[str, Any], zip_path: str, envelope_ok: bool = False) -> bool:
    """Download a source's feed into zip_path, for its creation or a refresh.

    Downloaded beside the current zip and swapped in only once complete
    and proven a feed: the zip is the only full record of the feed and
    must survive a failed or hijacked download. A host that stopped
    answering ranges sends the whole envelope: the network picked is taken
    out of it in stage_zip. False when it failed, said in the log, the zip
    as it was and no half-written copy left: the creation of a source had
    a copy of this of its own, and left one when a download broke off.
    """
    response, staged = download_feed(data, zip_path, envelope_ok)
    if response is None or staged is None:
        return False
    return keep_download(response, staged, zip_path, data.get("url"))


def _refresh_route_by_route(gtfs_dir: str, filename: str, zip_name: str, routes: list[str],
                            data: dict[str, Any]) -> dict[str, int] | bool:
    """Rebuild a source line by line: the routes its database follows now.

    The fresh feed is filtered down to those routes, unpacked into the
    scratch database, copied into a new real file and swapped in, as
    refresh_datasource tells. Returns {route_id: stop_times}, or False.
    """
    real = real_path(gtfs_dir, filename)
    # the new real database is built under its own datasource name, so every
    # existing helper works on it unchanged and nothing it does can touch the
    # file the sensors are reading
    staging = staging_name(filename)
    new_real = real_path(gtfs_dir, staging)

    def _build(scratch_file: str) -> bool:
        return build_scratch_database(
            gtfs_dir, zip_name, scratch_file,
            data.get("clean_feed_info", False), only_routes=routes)

    if not _stale_staging_gone(new_real, filename):
        return False
    try:
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
        remove_database(new_real)
    _LOGGER.info("Refreshed datasource %s from its source: %s stop_times "
                 "per route", filename, added)
    return added


def refresh_datasource(hass: HomeAssistant, path: str,
                       data: dict[str, Any]) -> dict[str, int] | dict[str, None] | bool:
    """Refresh a datasource from its source, keeping the sensors served.

    The legacy update rebuilt the real database in place: on a large feed
    the sensors read a half-built file for as long as the import took. Here
    the fresh feed is filtered down to the routes the database actually
    follows, unpacked into the scratch database, copied into a new real
    file built beside the old one, and the two are swapped in one rename.
    The coordinators reopen the file on their next cycle, so they only ever
    see the old complete data or the new complete data.

    A source whose database is gone or empty while its sensors name their
    lines gets those lines back, route by route. One that follows no line
    at all, a new source or one a first import left empty with no sensor
    yet, is built whole, the way the sources a train or local
    stops sensor reads are refreshed: every line of the feed, into a new
    file swapped in. It used to go down the legacy extract, which deleted
    the database and the zip and rebuilt the network in place, in a forked
    process that outlived the source lock.

    data may carry read_routes, the lines the source's sensors name: the
    new edition must still carry trips for those, or the swap is refused.

    Returns {route_id: stop_times} on success ({route_id: None} for a
    whole build), False on failure.
    """
    gtfs_dir = hass.config.path(path)
    filename = data["file"]
    real = real_path(gtfs_dir, filename)
    loaded = routes_in(real)
    if loaded is None:
        # the file is there but would not answer: something holds it, a
        # VACUUM or an intern, or it is momentarily unreadable. Reading that
        # as "follows no route" would rebuild the whole network over it
        _LOGGER.error("Cannot read the routes of %s, keeping its data", filename)
        return False
    routes = sorted(loaded)
    if not routes and not data.get("whole_feed") and data.get("read_routes"):
        # the database is gone or came out empty, and the lines it followed
        # went with it: the sensors still name theirs. Built whole, a source
        # cut down to a few lines of TAO or IDFM came back as the network
        routes = sorted(data["read_routes"])
        _LOGGER.info("Datasource %s holds no line, building the %s its sensors read",
                     filename, len(routes))
    whole = data.get("whole_feed") or not routes
    if data.get("whole_feed"):
        # the reason it is built whole, whatever the database held: a
        # source with train entries was said to follow no route (Zou,
        # field test of 98c023a)
        _LOGGER.info("Datasource %s is read whole by a train, local stops or "
                     "line-less sensor, building it whole", filename)
    elif not routes:
        _LOGGER.info("Datasource %s follows no route yet, building it whole",
                     filename)

    zip_path = feed_zip(gtfs_dir, filename)
    zip_name = os.path.basename(zip_path)
    if data.get("extract_from", "url") == "url" and not _fetch_zip(data, zip_path):
        return False
    if not os.path.exists(zip_path):
        _LOGGER.error("No source zip to refresh %s from", filename)
        return False
    if data.get("check_source_dates", False) and zip_only_future_dates(zip_path):
        _LOGGER.info("New file contains only dates in the future, "
                     "keeping the current data")
        return False

    if whole:
        return _refresh_whole_feed(gtfs_dir, filename, zip_name, zip_path, data)
    return _refresh_route_by_route(gtfs_dir, filename, zip_name, routes, data)
