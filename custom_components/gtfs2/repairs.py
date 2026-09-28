"""The fixes Settings > Repairs offers for gtfs2's issues.

A failed update of a source is tried again (RetryRefreshFlow); a line no
sensor reads any more has its timetable dropped from its source, that line
alone (DropLineFlow). The issues themselves are raised and cleared in
notifications.py. Home Assistant removes an issue when its fix ends with
an entry, and keeps it when the fix aborts.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow

from .datasource_services import async_prune_line
from .rt_source import datasource_entry
from .source_refresh import async_refresh_source


class RetryRefreshFlow(RepairsFlow):
    """Try a source's failed update again, now."""

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        file = self.data["file"]
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}),
                                        description_placeholders={"file": file})
        entry = datasource_entry(self.hass, file)
        if entry is None:
            return self.async_abort(reason="source_gone")
        # a rebuild takes minutes: it runs on its own, and raises the issue
        # again if it fails again
        self.hass.async_create_background_task(
            async_refresh_source(self.hass, entry), f"gtfs2 refresh {file}")
        return self.async_create_entry(data={})


class DropLineFlow(RepairsFlow):
    """Drop the timetable of a line no sensor reads any more."""

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        file, route, line = self.data["file"], self.data["route"], self.data["line"]
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}),
                                        description_placeholders={"file": file, "line": line})
        refused = await async_prune_line(self.hass, file, route)
        if refused:
            return self.async_abort(reason=refused)
        return self.async_create_entry(data={})


async def async_create_fix_flow(hass, issue_id, data):
    """The fix of one issue, by its kind."""
    if issue_id.startswith("refresh_failed_"):
        return RetryRefreshFlow()
    if issue_id.startswith("line_orphaned_"):
        return DropLineFlow()
    return ConfirmRepairFlow()
