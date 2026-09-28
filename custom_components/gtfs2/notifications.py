"""What the integration tells the user outside a config flow.

An import runs on after its flow window is closed: nobody is left in a
flow to read the outcome, so it is raised as a persistent notification in
the user's language, its strings under "common" in strings.json. What
lasts and can be acted on, a source that failed to update, an update
refused because lines went missing, a line no sensor reads any more, is a
repairs issue instead (strings under "issues"): it sits in Settings >
Repairs, offers its fix where there is one (repairs.py), and goes away
with its cause. Called from the config flow, from __init__ (entry setup
and removal) and from source_refresh.
"""
from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_notify_import(hass, filename, routes, added):
    """Report how an import went, for a user who closed the progress window.

    The import runs in the executor and reaches its end whatever happens to the
    flow, but an abandoned flow means nobody is left to say so. Called from a
    background task, which outlives it.

    The import stops at the first line that fails and does not try the
    ones after it. The lines that did not come in are named beside those
    that did: listed alone, the lines added read as all that was asked.
    """
    if not added:
        _LOGGER.error("Import into %s failed for %s", filename, routes)
        await _async_notify(hass, "import_failed", f"gtfs2_import_{filename}",
                            file=filename)
        return
    lines = ", ".join(r.split(":")[-1] for r in added)
    missing = [r for r in routes if r not in added]
    _LOGGER.info("Import into %s added %s", filename, added)
    if missing:
        _LOGGER.warning("Import into %s did not bring in %s", filename, missing)
        await _async_notify(hass, "import_partial", f"gtfs2_import_{filename}",
                            file=filename, lines=lines,
                            missing=", ".join(r.split(":")[-1] for r in missing))
        return
    await _async_notify(hass, "import_done", f"gtfs2_import_{filename}",
                        file=filename, lines=lines)


def line_orphaned_issue(filename, route):
    return f"line_orphaned_{filename}_{route}"


async def async_notify_line_orphaned(hass, filename, route, line):
    """Say that a line's last sensor is gone while its timetable remains.

    Raised by the entry removal hook. Deliberately not a prune: the user may
    be reshuffling sensors and want the line right back, so the issue names
    what is now dead weight, and its fix drops that line when the user asks
    (repairs.py): the choice stays theirs. One issue per line: two sensors
    removed one after the other used to leave only the second line named.
    route is the line's id, line its label as the sensor named it.
    """
    _LOGGER.info("No sensor reads line %s of %s any more", line, filename)
    ir.async_create_issue(
        hass, DOMAIN, line_orphaned_issue(filename, route),
        is_fixable=True, is_persistent=True, severity=ir.IssueSeverity.WARNING,
        translation_key="line_orphaned",
        translation_placeholders={"file": filename, "line": line},
        data={"file": filename, "route": route, "line": line})


def clear_line_orphaned(hass, filename, route):
    """A sensor reads the line again: it is no dead weight any more."""
    ir.async_delete_issue(hass, DOMAIN, line_orphaned_issue(filename, route))


async def async_notify_lines_missing(hass, filename, routes):
    """Say that a refresh was refused: the new edition lost lines sensors read.

    The current timetable stays, so the sensors keep running on it; what
    is left to the user is telling a renumbered line from a retired one,
    which no feed says, so the issue has no fix of its own. It replaces a
    failure of another kind, and a refresh that goes through clears it.
    """
    lines = ", ".join(r.split(":")[-1] for r in routes)
    ir.async_delete_issue(hass, DOMAIN, f"refresh_failed_{filename}")
    ir.async_create_issue(
        hass, DOMAIN, f"lines_missing_{filename}",
        is_fixable=False, is_persistent=True, severity=ir.IssueSeverity.WARNING,
        translation_key="lines_missing",
        translation_placeholders={"file": filename, "lines": lines})


async def async_notify_refresh(hass, filename, ok, lines_missing=None):
    """Say how a rebuild of a source ended, when it did not go through.

    A refresh started by the nightly check has nobody watching: failed, it
    said nothing, and the source stayed on its old edition unnoticed. The
    failure is an issue whose fix tries again; a rebuild that goes through
    clears what an earlier one left.
    """
    if ok:
        for kind in ("refresh_failed", "lines_missing"):
            ir.async_delete_issue(hass, DOMAIN, f"{kind}_{filename}")
    elif lines_missing:
        await async_notify_lines_missing(hass, filename, lines_missing)
    else:
        ir.async_delete_issue(hass, DOMAIN, f"lines_missing_{filename}")
        ir.async_create_issue(
            hass, DOMAIN, f"refresh_failed_{filename}",
            is_fixable=True, is_persistent=True, severity=ir.IssueSeverity.ERROR,
            translation_key="refresh_failed",
            translation_placeholders={"file": filename},
            data={"file": filename})


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
