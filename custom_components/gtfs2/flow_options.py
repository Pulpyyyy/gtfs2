"""The options screens this fork adds to an entry.

A datasource entry carries the realtime feeds and the refresh of its
source for every sensor of the source: the menu that leads to them, the
key screen of the realtime feeds, and the static refresh screens (how
often to ask the host whether the zip changed, and with which key). The
schemas come from flow_source, the same fields as when the source was
created. Mixed in GTFSOptionsFlowHandler.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_API_KEY,
    CONF_EXTRACT_FROM,
    CONF_NEEDS_API_KEY,
    CONF_STATIC_CHECK_INTERVAL,
    CONF_STATIC_REFRESH_MODE,
    CONF_URL,
    DEFAULT_STATIC_CHECK_INTERVAL,
    MAX_STATIC_CHECK_INTERVAL,
    MIN_STATIC_CHECK_INTERVAL,
    STATIC_REFRESH_MODES,
    STATIC_REFRESH_OFF,
    TRANSLATION_DESCRIPTION_PLACEHOLDERS,
)
from .flow_source import _collect_source_rt_options, _source_key_schema, _source_rt_key_schema, _typed_key
from .rt_source import STATIC_KEY_KEYS, static_feed_config, static_key_fields

_LOGGER = logging.getLogger(__name__)


class OptionsScreens:
    """The datasource options: menu, realtime key, static refresh and its key."""

    async def async_step_source_menu(
           self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """The datasource entry's CONFIGURE button: everything a source owns.

        Its realtime feeds, and its static feed with where it comes from
        and what to do about new versions. A step of its own, so the menu
        reads its own words and not the journey screen's.
        """
        return self.async_show_menu(
            step_id="source_menu",
            menu_options=["real_time", "static_refresh"])

    async def async_step_real_time_key(
           self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the realtime api key, only when the source needs one."""
        errors: dict[str, str] = {}
        opts = self.config_entry.options
        if user_input is None:
            return self.async_show_form(
                step_id="real_time_key",
                data_schema=vol.Schema(_source_rt_key_schema(opts)),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )
        _LOGGER.debug("UserInput Source realtime key received")
        return self.async_create_entry(
            title="", data=_collect_source_rt_options(
                self._user_inputs, _typed_key(user_input, opts),
                previous=self.config_entry.options))

    async def async_step_static_refresh(
           self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """The source's static feed: where it comes from, what to do about
        new versions.

        Off by default: nothing changes for anyone who does not come here.
        The user picks a frequency, never a moment: the moment is derived
        in source_refresh, at night and staggered per source.

        The feed address and its key live here too: the file name is the
        source's identity and never changes, but providers move and
        renumber their download urls and rotate their keys, so both must be
        editable in place, and this is the one place they are. What the
        creation flow asked once is edited here with the same screens; the
        refresh paths read the source, never a caller. The key stays
        behind its toggle like everywhere else. The change lands on the
        datasource entry's data and the mirror listener writes it through
        to the journey entries, so a downgrade to upstream keeps working.
        """
        errors: dict[str, str] = {}
        opts = self.config_entry.options
        current = static_feed_config(self.hass, self.config_entry)
        if user_input is not None:
            new_url = (user_input.get(CONF_URL) or "").strip()
            if new_url and not new_url.startswith(("http://", "https://")):
                errors[CONF_URL] = "invalid_source_url"
            else:
                self._user_inputs = {**user_input, CONF_URL: new_url}
                if self._user_inputs.pop(CONF_NEEDS_API_KEY, False):
                    return await self.async_step_static_refresh_key()
                return self._finish_static_refresh({})
        url = current.get(CONF_URL)
        return self.async_show_form(
            step_id="static_refresh",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_URL,
                    default="" if url in (None, "na") else url,
                ): str,
                # the three key fields only matter for the few sources that
                # need one, so they live behind this toggle
                vol.Optional(
                    CONF_NEEDS_API_KEY,
                    default=bool(current.get(CONF_API_KEY)),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_STATIC_REFRESH_MODE,
                    default=opts.get(CONF_STATIC_REFRESH_MODE,
                                     STATIC_REFRESH_OFF),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=STATIC_REFRESH_MODES,
                        translation_key="static_refresh_mode",
                    )
                ),
                vol.Required(
                    CONF_STATIC_CHECK_INTERVAL,
                    default=opts.get(CONF_STATIC_CHECK_INTERVAL,
                                     DEFAULT_STATIC_CHECK_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_STATIC_CHECK_INTERVAL,
                        max=MAX_STATIC_CHECK_INTERVAL,
                        step=1,
                        unit_of_measurement="h",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }),
            description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
            errors=errors,
        )

    async def async_step_static_refresh_key(
           self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the static feed's key, only when the source needs one."""
        if user_input is None:
            return self.async_show_form(
                step_id="static_refresh_key",
                data_schema=vol.Schema(_source_key_schema(
                    static_feed_config(self.hass, self.config_entry))),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors={},
            )
        _LOGGER.debug("UserInput Source static key received")
        return self._finish_static_refresh(_typed_key(
            user_input, static_feed_config(self.hass, self.config_entry)))

    def _finish_static_refresh(self, key_fields) -> FlowResult:
        """Store what the static feed screens collected.

        The address and the key go on the datasource entry's data, the
        refresh policy in its options. Untoggling the key drops it: the
        mirror listener takes it off the journey entries too. An address
        given to a zip source makes it a hosted one, checkable from now on,
        which is what the bootstrap would have done at the next start.
        """
        fields = self._user_inputs
        entry = self.config_entry
        new_data = {**entry.data}
        for key in STATIC_KEY_KEYS:
            new_data.pop(key, None)
        new_data.update(static_key_fields(key_fields))
        if fields.get(CONF_URL):
            new_data[CONF_URL] = fields[CONF_URL]
            if new_data.get(CONF_EXTRACT_FROM) == "zip":
                new_data[CONF_EXTRACT_FROM] = "url"
        if new_data != dict(entry.data):
            self.hass.config_entries.async_update_entry(entry, data=new_data)
        return self.async_create_entry(
            title="", data={
                **entry.options,
                CONF_STATIC_REFRESH_MODE: fields[CONF_STATIC_REFRESH_MODE],
                CONF_STATIC_CHECK_INTERVAL: int(fields[CONF_STATIC_CHECK_INTERVAL]),
            })
