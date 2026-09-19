"""What the integration tells the user outside a config flow.

An extraction or an import runs on after its flow window is closed, and an
entry removal can leave a line's timetable without a sensor. Nobody is left
in a flow to read the outcome, so it is raised as a persistent notification
in the user's language: the strings live under "common" in strings.json.
check_extraction_result is the judge the watcher relies on: whether the
unpacking really produced a usable datasource. Called from the config flow,
from __init__ (entry removal) and from source_refresh.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3

from homeassistant.core import HomeAssistant
from homeassistant.components import persistent_notification
from homeassistant.helpers.translation import async_get_translations

from .const import DEFAULT_PATH, DOMAIN
from .gtfs_helper import check_extracting

_LOGGER = logging.getLogger(__name__)


def check_extraction_result(gtfs_dir, filename):
    """Whether an extraction actually produced a usable datasource.

    check_extracting only says that nothing is writing any more, which a
    process killed halfway satisfies just as well as one that succeeded: both
    leave no journal behind. So finishing has to be told apart from working.

    pygtfs writes the _feed row last, once every table is loaded, which makes
    it the honest marker. The counts confirm the tables a journey needs are
    populated, since a feed with no routes or no stop_times cannot answer any
    query the integration makes.

    Returns (ok, detail). On success detail holds the row counts; on failure
    it is a reason code, so the notification can word it in the user's
    language rather than repeat an English sentence built here.
    """
    sqlite_file = os.path.join(gtfs_dir, filename + ".sqlite")
    if not os.path.exists(sqlite_file):
        return False, "no_database"
    try:
        conn = sqlite3.connect(sqlite_file, timeout=10)
    except sqlite3.Error as ex:
        _LOGGER.error("Cannot open %s: %s", sqlite_file, ex)
        return False, "cannot_open"
    try:
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute(
            "select name from sqlite_master where type in ('table', 'view')")}
        missing = {"_feed", "routes", "trips", "stops", "stop_times"} - tables
        if missing:
            _LOGGER.error("Missing tables in %s: %s", filename, sorted(missing))
            return False, "tables_missing"
        # the _feed row is written once everything else is in
        if not cur.execute("select count(*) from _feed").fetchone()[0]:
            return False, "unfinished"
        counts = {t: cur.execute(f"select count(*) from {t}").fetchone()[0]  # noqa: S608
                  for t in ("routes", "trips", "stops", "stop_times")}
        empty = [t for t, n in counts.items() if not n]
        if empty:
            _LOGGER.error("Empty tables in %s: %s", filename, empty)
            return False, "tables_empty"
    except sqlite3.Error as ex:
        _LOGGER.error("Cannot read %s: %s", sqlite_file, ex)
        return False, "unreadable"
    finally:
        conn.close()
    _LOGGER.debug("Extraction of %s looks complete: %s", filename, counts)
    # the counts themselves, so the notification can word them in the user's
    # language: "43 routes, 679988 stop_times" is table names, untranslatable
    # and of no use to whoever reads it
    return True, counts


async def async_watch_extraction(hass: HomeAssistant, filename: str):
    """Wait for an extraction to end, then say how it went.

    The unpacking runs in a detached process, which has its own copy of hass
    and no event loop: it cannot raise a notification itself, and anything it
    creates dies with it. So the watching is done here, and the only thing that
    crosses the boundary is the state of the files on disk.

    This exists because the config flow is not a reliable witness. Closing its
    window abandons the flow while the extraction carries on, leaving no way to
    learn that it finished, or whether it worked.
    """
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    # every source goes through the progress step now, including one already
    # unpacked. Nothing happened there, so there is nothing to announce.
    ok, _ = await hass.async_add_executor_job(
        check_extraction_result, gtfs_dir, filename)
    if ok and not await hass.async_add_executor_job(
        check_extracting, hass, DEFAULT_PATH, filename
    ):
        _LOGGER.debug("Nothing to watch for %s, it is already built", filename)
        return
    # the fork needs a moment before it creates the journal, so a datasource
    # that does not look busy yet is not necessarily done
    await asyncio.sleep(10)
    while await hass.async_add_executor_job(
        check_extracting, hass, DEFAULT_PATH, filename
    ):
        await asyncio.sleep(15)

    ok, detail = await hass.async_add_executor_job(
        check_extraction_result, gtfs_dir, filename)
    # Whoever reads this closed the progress window: they left before knowing
    # how it went, and have to pick the flow back up by hand. So say where to
    # go, not just what happened.
    if ok:
        _LOGGER.info("Extraction of %s finished: %s", filename, detail)
        await _async_notify(hass, "extract_ready", f"gtfs2_extract_{filename}",
                            file=filename, routes=detail.get("routes", 0),
                            stops=detail.get("stops", 0))
    else:
        _LOGGER.error("Extraction of %s failed: %s", filename, detail)
        # the reason is a key of its own, so the whole message is translated
        # rather than a translated frame around an English sentence
        reason = await _async_text(hass, f"reason_{detail}", detail)
        await _async_notify(hass, "extract_failed", f"gtfs2_extract_{filename}",
                            file=filename, detail=reason)


async def async_notify_import(hass, filename, routes, added):
    """Report how an import went, for a user who closed the progress window.

    The import runs in the executor and reaches its end whatever happens to the
    flow, but an abandoned flow means nobody is left to say so. Called from a
    background task, which outlives it.
    """
    if not added:
        _LOGGER.error("Import into %s failed for %s", filename, routes)
        await _async_notify(hass, "import_failed", f"gtfs2_import_{filename}",
                            file=filename)
        return
    lines = ", ".join(r.split(":")[-1] for r in added)
    _LOGGER.info("Import into %s added %s", filename, added)
    await _async_notify(hass, "import_done", f"gtfs2_import_{filename}",
                        file=filename, lines=lines)


async def async_notify_line_orphaned(hass, filename, line):
    """Say that a line's last sensor is gone while its timetable remains.

    Raised by the entry removal hook. Deliberately not a prune: the user may
    be reshuffling sensors and want the line right back, so the notification
    names what is now dead weight and the service that drops it, and the
    choice stays theirs.
    """
    _LOGGER.info("No sensor reads line %s of %s any more", line, filename)
    await _async_notify(hass, "line_orphaned", f"gtfs2_prune_{filename}",
                        file=filename, line=line)


async def async_notify_lines_missing(hass, filename, routes):
    """Say that a refresh was refused: the new edition lost lines sensors read.

    Raised by the refresh itself. The current timetable stays, so the
    sensors keep running on it; what is left to the user is telling a
    renumbered line from a retired one, which no feed says. Same id as the
    orphaned-line notification: one notification per source sums up the
    state of its lines.
    """
    lines = ", ".join(r.split(":")[-1] for r in routes)
    await _async_notify(hass, "lines_missing", f"gtfs2_prune_{filename}",
                        file=filename, lines=lines)


async def _async_notify(hass, key, notification_id, **values):
    """Raise a notification in the user's language.

    A notification is read outside any config flow, so it cannot lean on the
    placeholders Home Assistant fills there: the strings are fetched and
    formatted here. They live under the "common" section of strings.json,
    alongside the flow's own, so translating the integration covers them too
    (hassfest knows no "notification" section, and rejects one).

    Falls back to the key itself when a translation is missing, which is
    visible without being fatal.
    """
    persistent_notification.async_create(
        hass,
        await _async_text(hass, key, key, **values),
        title=await _async_text(hass, f"{key}_title", "GTFS", **values),
        notification_id=notification_id)


async def _async_text(hass, name, default, **values):
    """One notification string, in the user's language, placeholders filled."""
    try:
        strings = await async_get_translations(
            hass, hass.config.language, "common", {DOMAIN})
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not load notification strings: %s", ex)
        strings = {}
    raw = strings.get(f"component.{DOMAIN}.common.{name}", default)
    try:
        return raw.format(**values)
    except (KeyError, IndexError):
        return raw
