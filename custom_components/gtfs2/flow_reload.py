"""The screens that load lines into a datasource, and shrink it.

A source is served from its zip and only the lines the user picks are
imported: the route screen sends a line that has no timetable yet through
route_reload, which offers the other lines of the operator to load along,
then importing shows the progress and reload_done or reload_failed says
how it went. optimise drops what no sensor reads any more. Mixed in
ConfigFlow; every method reads and writes the flow's own state (self).
"""
# mixin: The import screens: which lines to load, the progress, the outcome, and the optimise screen.
from __future__ import annotations

import asyncio
import logging
import os

import voluptuous as vol

from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_AGENCY,
    CONF_ALSO_RELOAD,
    CONF_FILE,
    CONF_KIND,
    CONF_ROUTE,
    DEFAULT_PATH,
    DOMAIN,
    ENTRY_KIND_DATASOURCE,
    TRANSLATION_DESCRIPTION_PLACEHOLDERS,
)
from .gtfs_db import import_routes, optimise_datasource, real_path, routes_in
from .gtfs_helper import check_datasource_index
from .notifications import async_notify_import
from .route_names import get_route_labels, get_route_labels_from_zip, get_routes_in_zip, routes_in_zip_for_agency
from .source_refresh import source_lock
from .source_zip import build_scratch_database, open_datasource

_LOGGER = logging.getLogger(__name__)


def _database_size(gtfs_dir, filename):
    """How big the datasource is right now, as a readable string.

    An extraction only ever adds rows, so the file grows steadily and its size
    is the one honest sign of progress available from outside the fork. There
    is no total to compare against, since it depends on the network.
    """
    for suffix in (".import.sqlite", ".sqlite"):
        path = os.path.join(gtfs_dir, filename + suffix)
        if os.path.exists(path):
            try:
                return f"{os.path.getsize(path) / 1048576:.0f} MB"
            except OSError:
                break
    return "0 MB"


def _scratch_size(gtfs_dir, filename):
    """How big the import database has grown, as a readable string.

    Only the scratch file: the real datasource is not being written during an
    import, so reporting its size would show a figure that never moves.
    """
    path = os.path.join(gtfs_dir, filename + ".import.sqlite")
    try:
        return f"{os.path.getsize(path) / 1048576:.0f} MB"
    except OSError:
        return "0 MB"


class ReloadScreens:
    """The import screens: which lines to load, the progress, the outcome, and the optimise screen."""

    async def async_step_route_reload_only(self, user_input: dict | None = None) -> FlowResult:
        """The reload screen when no other line is missing.

        Same step, different wording: only the step_id changes, so this hands
        straight over. Without a method of this name Home Assistant refuses the
        flow the moment the user submits it.
        """
        return await self.async_step_route_reload(user_input)

    async def async_step_route_reload(self, user_input: dict | None = None) -> FlowResult:
        """Offer to bring the line back, and any other missing one at the same time.

        Unpacking the feed is the slow part, minutes on a large network, and it
        costs the same whether one line or ten are copied out of it. Adding
        three lines one after the other would pay that price three times, so
        the other missing lines are offered here.
        """
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        filename = self._user_inputs[CONF_FILE]
        route_id = self._user_inputs[CONF_ROUTE]

        if user_input is None:
            # every line the feed declares but the datasource has no trips for,
            # minus the one being added, which is not optional here
            in_zip = await self.hass.async_add_executor_job(
                get_routes_in_zip, gtfs_dir, filename)
            loaded = await self.hass.async_add_executor_job(
                routes_in, real_path(gtfs_dir, filename))
            if loaded is None:
                # the database would not answer (busy, being rebuilt): which
                # lines it lacks cannot be told, so only the one picked goes in
                _LOGGER.warning("Cannot read the lines of %s, offering none to add", filename)
                loaded = set(in_zip)
            missing = sorted(in_zip - loaded - {route_id})
            # the operator was named on the agency screen: offer that
            # operator's missing lines, not the whole feed's
            agency = self._user_inputs.get(CONF_AGENCY, "0: ALL").split(': ')[0]
            missing = await self.hass.async_add_executor_job(
                routes_in_zip_for_agency, gtfs_dir, filename, missing, agency)
            if self._pygtfs and hasattr(self._pygtfs, 'session'):
                labels = await self.hass.async_add_executor_job(
                    get_route_labels, self._pygtfs, missing, gtfs_dir, filename)
            else:
                # a fresh source has no database to ask yet
                labels = await self.hass.async_add_executor_job(
                    get_route_labels_from_zip, gtfs_dir, filename, missing)
            schema = {}
            if labels:
                # checkboxes read well for a handful of missing lines; a
                # national feed leaves thousands, which only a searchable
                # dropdown survives
                mode = (selector.SelectSelectorMode.LIST if len(labels) <= 25
                        else selector.SelectSelectorMode.DROPDOWN)
                schema[vol.Optional(CONF_ALSO_RELOAD, default=[])] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[selector.SelectOptionDict(value=r, label=l)
                                 for r, l in labels.items()],
                        multiple=True, mode=mode,
                    ))
            # with nothing else missing, the paragraph about other lines would
            # read "0 other lines": use the wording that does not mention them
            return self.async_show_form(
                step_id="route_reload" if labels else "route_reload_only",
                data_schema=vol.Schema(schema),
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "route": self._route_label,
                    "missing": str(len(labels)),
                },
            )
        # Bring back the wanted lines. The zip is unpacked into a scratch
        # database the sensors never open, the lines are copied across, and the
        # scratch file is dropped: the other sensors keep reading a complete
        # datasource throughout, and nothing else that was pruned comes back.
        self._import_routes = [route_id] + [
            r for r in user_input.get(CONF_ALSO_RELOAD, []) if r != route_id]
        return await self.async_step_importing()

    async def async_step_importing(self, user_input: dict | None = None) -> FlowResult:
        """Run the import while showing how far the scratch database has grown.

        Unpacking takes minutes on a large network. Calling it straight from a
        step leaves Home Assistant waiting on the coroutine with nothing on
        screen, so the work goes in a task and this step reports on it.
        """
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        filename = self._user_inputs.get(CONF_FILE, "")

        if self._import_job is None:
            clean = self._user_inputs.get("clean_feed_info", False)
            routes = list(self._import_routes)

            def _build(scratch_file):
                # the feed is filtered down to the wanted lines before pygtfs
                # sees it: on a national feed this is what turns the import
                # from an hour into a minute
                return build_scratch_database(
                    gtfs_dir, filename + ".zip", scratch_file, clean,
                    only_routes=routes)

            # The work runs in the executor and finishes whatever happens to
            # this window, but nobody would hear about it once the flow is
            # abandoned. A background task outlives the flow and reports the
            # outcome, which is what the screen promises.
            async def _watch():
                try:
                    added = await self._import_job
                except Exception:  # pylint: disable=broad-except
                    # said by the step when it reads the job; the rider who
                    # closed the window hears it here, as a failed import
                    added = None
                await async_notify_import(self.hass, filename, routes, added)

            async def _import():
                # behind the source's own lock: a refresh rebuilds the file
                # beside this one and swaps it in, which would take the lines
                # added here with it. The wait shows as the progress screen.
                async with source_lock(self.hass, filename):
                    return await self.hass.async_add_executor_job(
                        import_routes, gtfs_dir, filename, routes, _build)

            self._import_job = self.hass.async_create_task(_import())
            self.hass.async_create_background_task(
                _watch(), name=f"gtfs2 watch import {filename}")

        if not self._import_job.done():
            # Home Assistant redraws a progress screen only when its
            # progress_task finishes, never while it runs. So the task handed
            # over is a short wait, not the import itself: it ends every few
            # seconds, the step is called again, and the size is read afresh.
            # Watching the import directly would freeze the figure at 0 - the
            # scratch file does not exist yet when the screen first appears.
            self._extract_size = await self.hass.async_add_executor_job(
                _scratch_size, gtfs_dir, filename)

            async def _tick():
                await asyncio.wait({self._import_job}, timeout=3)

            self._import_task = self.hass.async_create_task(_tick())
            return self.async_show_progress(
                step_id="importing",
                progress_action="importing",
                progress_task=self._import_task,
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "file": filename,
                    "size": self._extract_size,
                    "routes": str(len(self._import_routes)),
                },
            )

        try:
            added = self._import_job.result()
        except Exception as ex:  # pylint: disable=broad-except
            # an import that raised, rather than one that returned nothing:
            # read bare, it took the step down with an unknown error
            _LOGGER.error("Import into %s failed: %s", filename, ex)
            added = None
        self._import_job = None
        self._import_task = None
        if not added:
            return self.async_show_progress_done(next_step_id="reload_failed")
        return self.async_show_progress_done(next_step_id="reload_done")

    async def async_step_reload_failed(self, user_input: dict | None = None) -> FlowResult:
        """The import could not run: the datasource is untouched.

        Back to the line picker rather than out of the flow. The source and the
        operator are still valid - only this one line could not be brought
        back - and abandoning would throw away everything chosen since, for a
        failure that says nothing about the rest.
        """
        self._pending_error = "reload_failed"
        return await self.async_step_route()

    async def async_step_reload_done(self, user_input: dict | None = None) -> FlowResult:
        """Carry on picking the journey, with the lines now loaded."""
        # reopen directly, without get_gtfs's extracting gate: the import
        # just succeeded so the file exists, and a coordinator adding an
        # index at this very moment leaves a journal that the gate mistakes
        # for an unpacking still running. Measured in the field: get_gtfs
        # answered "extracting" and the direction screen crashed on a string.
        self._pygtfs = await self.hass.async_add_executor_job(
            open_datasource, self.hass.config.path(DEFAULT_PATH),
            self._user_inputs[CONF_FILE])
        if not self._pygtfs:
            return await self.async_step_reload_failed()
        # a fresh source was just created by this very import and never went
        # through the step that checks the indexes, so check them here
        await self.hass.async_add_executor_job(
            check_datasource_index, self.hass, self._pygtfs, DEFAULT_PATH,
            self._user_inputs[CONF_FILE])
        return await self.async_step_direction()

    async def async_step_optimise(self, user_input: dict | None = None) -> FlowResult:
        """Shrink the datasource: drop unfollowed lines, then intern the rest.

        Both steps in one go, in that order: pruning first leaves interning
        less to rewrite. Measured on two Orleans lines, 85.7 MB became 32.1 MB.
        """
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        filename = self._user_inputs.get(CONF_FILE, "")
        # only the routes some entry actually reads: anything else in the file
        # is weight nothing queries
        keep = {e.data["route"].split(": ")[0]
                for e in self.hass.config_entries.async_entries(DOMAIN)
                if e.data.get("file") == filename and e.data.get("route")
                and not e.data.get("device_tracker_id")}
        # the entries created by this flow are made through separate flows, so
        # they are not guaranteed to be registered yet. Without this, the line
        # just added could be pruned away moments after being imported.
        if self._user_inputs.get(CONF_ROUTE):
            keep.add(self._user_inputs[CONF_ROUTE].split(": ")[0])
        if self._return_trip and self._return_trip.get(CONF_ROUTE):
            keep.add(self._return_trip[CONF_ROUTE].split(": ")[0])
        # same reasoning for the lines this flow imported alongside: their
        # sensors come in the next flows, and dropping them here would undo
        # an import the user asked for minutes ago
        keep.update(self._import_routes)
        unrestricted = any(
            e.data.get("device_tracker_id") or not e.data.get("route")
            # "train" is a marker, not a route_id: a train sensor matches
            # city pairs across the whole feed, so nothing may be dropped
            or e.data.get("route") == "train"
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if e.data.get("file") == filename
            # the datasource entry reads nothing: it must not make its own
            # source look unrestricted
            and e.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE)

        if user_input is None:
            size = await self.hass.async_add_executor_job(
                _database_size, gtfs_dir, filename)
            # name what goes and what stays: "frees space" is not enough to
            # accept losing timetables, and the count of each is what decides
            loaded = await self.hass.async_add_executor_job(
                routes_in, real_path(gtfs_dir, filename))
            if loaded is None:
                # nothing to count, and nothing an optimisation could read
                _LOGGER.error("Cannot read the lines of %s, it is not optimised", filename)
                return self.async_abort(reason="generic_failure")
            dropped = {} if unrestricted else {r: 1 for r in loaded if r not in keep}
            # counted, not named: a network drops dozens of lines here and the
            # list buried the two figures that decide it
            return self.async_show_form(
                step_id="optimise",
                data_schema=vol.Schema({}),
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "file": filename,
                    "size": size,
                    "kept": str(len(keep & loaded) if not unrestricted else len(loaded)),
                    "dropped": str(len(dropped)),
                },
            )
        async with source_lock(self.hass, filename):
            result = await self.hass.async_add_executor_job(
                optimise_datasource, gtfs_dir, filename,
                None if unrestricted else keep)
        _LOGGER.info("Optimised datasource %s: %s", filename, result)
        return self.async_abort(
            reason="optimised",
            description_placeholders={
                **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                "size": await self.hass.async_add_executor_job(
                    _database_size, gtfs_dir, filename),
            },
        )
