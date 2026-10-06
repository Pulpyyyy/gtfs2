"""The source screens of the config flow: where the timetable comes from.

A source is a zip, fetched from a url or already in the gtfs2 folder, with
or without an api key, with or without realtime feeds of its own.
The schema builders for the realtime and key fields live here too, the
options flow reuses them. Mixed in ConfigFlow; every method reads and
writes the flow's own state (self).
"""
# mixin: The screens that name a source: url or zip, its key, its realtime feeds.
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlsplit
from collections.abc import Callable, Mapping
from typing import Any

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    ATTR_API_KEY_LOCATIONS,
    CONF_ACCEPT_HEADER_PB,
    CONF_ALERTS_URL,
    CONF_API_KEY,
    CONF_API_KEY_LOCATION,
    CONF_API_KEY_NAME,
    CONF_EXTRACT_FROM,
    CONF_FILE,
    CONF_INNER_ZIP,
    CONF_NEEDS_API_KEY,
    CONF_RT_ENABLED,
    CONF_STATIC_CHECK_INTERVAL,
    CONF_STATIC_REFRESH_MODE,
    CONF_TRIP_UPDATE_URL,
    CONF_URL,
    CONF_VEHICLE_MAX_AGE,
    CONF_VEHICLE_POSITION_URL,
    DEFAULT_API_KEY_LOCATION,
    DEFAULT_API_KEY_NAME,
    DEFAULT_VEHICLE_MAX_AGE,
    DEFAULT_PATH,
    TRANSLATION_DESCRIPTION_PLACEHOLDERS,
)
from .flow_journey import _Step
from .feed.freshness import source_meta
from .file_url import FILE_SCHEME
from .feed.files import feed_zip, real_path, get_zipfiles
from .key_mask import note_key
from .feed.source_entries import async_ensure_datasource_entry, datasource_entry
from .source_refresh import source_zip_url
from .source_zip import ensure_source_zip

_LOGGER = logging.getLogger(__name__)

# what a source may be called: letters, digits, spaces, dashes, underscores.
# No separator, no dot, nothing a file name on Linux or Windows refuses
# what an address typed for a source may start with: a host, or a file
# on this machine, which fetch reads as a host (file_url)
SOURCE_URL_SCHEMES = ("http://", "https://", FILE_SCHEME)


def valid_feed_url(url: str) -> bool:
    """Whether a typed address can name a feed, by its syntax alone:
    http(s) with a host, its port a number when one is given, or file://
    with a path. Whether it answers is the download's to say."""
    url = url.strip()
    try:
        parts = urlsplit(url)
        # a port that is no number raises here, not in urlsplit
        parts.port
    except ValueError:
        return False
    if parts.scheme in ("http", "https"):
        return bool(parts.hostname) and not any(c.isspace() for c in url)
    if parts.scheme == "file":
        return bool(parts.path)
    return False

_SOURCE_NAME = re.compile(r"\w[\w\- ]*")


def _source_rt_schema(opts: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    """The realtime feeds screen of a source, prefilled with what it has.

    Shared between the creation flow and the datasource entry's options so
    the two places a source is edited can never drift apart. Every field is
    optional: emptied means removed.
    """
    return {
        vol.Optional(CONF_TRIP_UPDATE_URL, default=opts.get(CONF_TRIP_UPDATE_URL, "")): str,
        vol.Optional(CONF_VEHICLE_POSITION_URL, default=opts.get(CONF_VEHICLE_POSITION_URL, "")): str,
        vol.Optional(
            CONF_VEHICLE_MAX_AGE,
            default=opts.get(CONF_VEHICLE_MAX_AGE, DEFAULT_VEHICLE_MAX_AGE),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=1440, step=1, unit_of_measurement="min",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(CONF_ALERTS_URL, default=opts.get(CONF_ALERTS_URL, "")): str,
        # the three key fields only matter for the few feeds that need one
        vol.Optional(CONF_NEEDS_API_KEY, default=bool(opts.get(CONF_API_KEY))): selector.BooleanSelector(),
    }


def _source_key_schema(previous: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    """The api key trio, prefilled with what a feed already has.

    One screen for every feed that needs a key: the static feed at creation
    and on the source's screen, the realtime feeds with one more field. A
    stored "not_applicable" is not offered back: on this screen a key exists,
    so it goes somewhere. A stored key comes back in a password field: hidden
    on screen, shown on demand by the field's own eye.
    """
    location = previous.get(CONF_API_KEY_LOCATION)
    if location not in ("header", "query_string", "basic"):
        location = "query_string"
    return {
        vol.Required(CONF_API_KEY, default=previous.get(CONF_API_KEY) or ""): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        vol.Required(
            CONF_API_KEY_NAME,
            default=previous.get(CONF_API_KEY_NAME) or DEFAULT_API_KEY_NAME,
        ): cv.string,
        vol.Required(
            CONF_API_KEY_LOCATION,
            default=location,
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[l for l in ATTR_API_KEY_LOCATIONS if l != "not_applicable"],
                translation_key="api_key_location",
            )
        ),
    }


def _typed_key(key_fields: Mapping[str, Any]) -> dict[str, Any]:
    """The key screen's fields.

    The key is noted on the way, so the logs hide it from the moment it is
    typed, before any entry stores it.
    """
    key = key_fields.get(CONF_API_KEY)
    note_key(key)
    return {**key_fields, CONF_API_KEY: key}


def _source_rt_key_schema(opts: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    """The realtime api key screen, shown only when the source needs one."""
    return {
        **_source_key_schema(opts),
        # gtfs_rt_helper only sends this when the key is in a header
        vol.Optional(
            CONF_ACCEPT_HEADER_PB,
            default=opts.get(CONF_ACCEPT_HEADER_PB, False),
        ): selector.BooleanSelector(),
    }


def rt_url_errors(url_fields: Mapping[str, Any]) -> dict[str, str]:
    """The realtime addresses typed that are no valid address, by field,
    held to the static address's rule; an empty field is no feed."""
    return {key: "invalid_source_url"
            for key in (CONF_TRIP_UPDATE_URL, CONF_VEHICLE_POSITION_URL, CONF_ALERTS_URL)
            if (url_fields.get(key) or "").strip() and not valid_feed_url(url_fields[key])}


def _collect_source_rt_options(url_fields: Mapping[str, Any], key_fields: Mapping[str, Any],
                               previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The options a datasource entry stores: what was typed, nothing empty.

    An emptied field means removal, so blanks and stray spaces never make it
    into the options - the coordinators read absence as
    "this source does not have that feed". Without a key, none of the key
    fields survive either. The rt_enabled switch is not on these screens,
    so its position rides through an edit untouched.
    """
    options: dict[str, Any] = {}
    for key in (CONF_TRIP_UPDATE_URL, CONF_VEHICLE_POSITION_URL, CONF_ALERTS_URL):
        value = (url_fields.get(key) or "").strip()
        if value:
            options[key] = value
    # the vehicle age limit is kept only when it is not the default, which
    # the coordinator applies when the options say nothing
    max_age = url_fields.get(CONF_VEHICLE_MAX_AGE)
    if max_age is not None and int(max_age) != DEFAULT_VEHICLE_MAX_AGE:
        options[CONF_VEHICLE_MAX_AGE] = int(max_age)
    if (key_fields.get(CONF_API_KEY) or "").strip():
        options[CONF_API_KEY] = key_fields[CONF_API_KEY].strip()
        options[CONF_API_KEY_NAME] = key_fields.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME)
        options[CONF_API_KEY_LOCATION] = key_fields.get(CONF_API_KEY_LOCATION, "query_string")
        options[CONF_ACCEPT_HEADER_PB] = bool(key_fields.get(CONF_ACCEPT_HEADER_PB, False))
    if previous is not None:
        # the switch and the static refresh settings are not on these
        # screens: an edit of the feeds must ride them through untouched
        for key in (CONF_RT_ENABLED, CONF_STATIC_REFRESH_MODE,
                    CONF_STATIC_CHECK_INTERVAL):
            if key in previous:
                options[key] = previous[key]
    return options


class SourceScreens:
    """The screens that name a source: url or zip, its key, its realtime feeds."""

    # what these screens use of the flow they are mixed in (ConfigFlow)
    hass: HomeAssistant
    _user_inputs: dict
    _pending_error: str | None
    _source_step: str | None
    _inner_zips: list
    _source_rt_inputs: dict
    async_show_form: Callable[..., FlowResult]
    async_show_progress: Callable[..., FlowResult]
    async_show_progress_done: Callable[..., FlowResult]
    async_abort: Callable[..., FlowResult]
    async_step_user: _Step
    async_step_start_end: _Step
    async_step_agency: _Step

    async def async_step_user_empty(self, user_input: dict | None = None) -> FlowResult:
        """The first-run menu, when no datasource exists yet.

        A step rendered under its own step_id needs a method of that name:
        Home Assistant looks one up as soon as the user picks an entry, and
        refuses the whole flow with "doesn't support step user_empty" without
        it. Reached again when the last datasource goes away, which is how it
        surfaced: renaming the zip left the entries pointing at nothing.
        """
        return await self.async_step_user(user_input)

    async def _name_taken_elsewhere(self, name: str, url: str) -> bool:
        """Whether another source already goes by this name.

        The zip and the entry of a source are found by its name alone, so a
        second source typed under a name in use went on silently with the
        first one's data, its own url kept nowhere that counts. The same
        name with the same url is the same source added again, and passes.
        """
        entry = datasource_entry(self.hass, name)
        if entry is not None:
            return entry.data.get(CONF_URL) != url
        zip_path = feed_zip(self.hass.config.path(DEFAULT_PATH), name)
        if not await self.hass.async_add_executor_job(os.path.exists, zip_path):
            return False
        # a zip and no entry yet: a download from this very url, left when
        # the flow was closed before the entry was made, is the same source
        # added again. A zip the user dropped there has no sidecar and keeps
        # its name, as does one fetched from anywhere else
        recorded = (await self.hass.async_add_executor_job(source_meta, zip_path)).get("url")
        same = recorded and (recorded == url or recorded.startswith(url + ("&" if "?" in url else "?")))
        return not same

    async def async_step_source_url(self, user_input: dict | None = None) -> FlowResult:
        """Download the feed from a url."""
        errors: dict[str, str] = {}

        def _show(errors: dict[str, str], previous: dict | None = None) -> FlowResult:
            previous = previous or {}
            return self.async_show_form(
                step_id="source_url",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_URL, default=previous.get(CONF_URL, "")): str,
                        vol.Required(CONF_FILE, default=previous.get(CONF_FILE, "")): str,
                        # the three key fields only matter for the few sources that
                        # need one, so they live behind this toggle
                        vol.Optional(
                            CONF_NEEDS_API_KEY,
                            default=previous.get(CONF_NEEDS_API_KEY, bool(previous.get(CONF_API_KEY))),
                        ): selector.BooleanSelector(),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            if self._take_pending_error(errors):
                # back from the key screen: what was typed is shown again,
                # so the url or the name can be put right
                return _show(errors, self._user_inputs)
            return _show(errors)
        # the name becomes the source's file name: every path of the source
        # is built from it, so "../x" wrote outside the gtfs2 folder, and a
        # dot made the list of sources cut it short and invent another one
        name = str(user_input.get(CONF_FILE) or "").strip()
        url = str(user_input.get(CONF_URL) or "").strip()
        errors = await self._source_url_errors(name, url)
        if errors:
            return _show(errors, user_input)
        user_input[CONF_FILE], user_input[CONF_URL] = name, url
        user_input[CONF_EXTRACT_FROM] = "url"
        self._source_step = "source_url"
        if user_input.pop(CONF_NEEDS_API_KEY, False):
            self._user_inputs.update(user_input)
            return await self.async_step_source_key()
        # a key typed before the toggle was turned off goes with it
        for key in (CONF_API_KEY, CONF_API_KEY_NAME):
            self._user_inputs.pop(key, None)
        user_input[CONF_API_KEY_LOCATION] = DEFAULT_API_KEY_LOCATION
        # only the zip is fetched here: importing waits until the lines are
        # chosen, so a national feed no longer means unpacking the whole
        # network before the first screen that asks what to keep
        check_data = await self.hass.async_add_executor_job(
            ensure_source_zip, self.hass, DEFAULT_PATH, user_input)
        if check_data:
            if check_data == "zip_holds_zips":
                # the source is an envelope of networks: which one it
                # follows is asked before anything is downloaded
                self._inner_zips = user_input.pop("inner_zips", [])
                self._user_inputs.update(user_input)
                return await self.async_step_inner_zip()
            errors["base"] = check_data
            return _show(errors, user_input)
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Source url: {self._user_inputs}")
        return await self.async_step_source_rt()

    async def _source_url_errors(self, name: str, url: str) -> dict[str, str]:
        """What is wrong with a typed source name and address, by field;
        empty when nothing is."""
        errors: dict[str, str] = {}
        if not _SOURCE_NAME.fullmatch(name):
            errors[CONF_FILE] = "invalid_source_name"
        if not valid_feed_url(url):
            errors[CONF_URL] = "invalid_source_url"
        if not errors and await self._name_taken_elsewhere(name, url):
            errors[CONF_FILE] = "source_exists"
        return errors

    async def async_step_inner_zip(self, user_input: dict | None = None) -> FlowResult:
        """Pick which network a source that holds several is built from.

        Some publishers answer one zip holding one zip per network, SEPTA's
        bus and rail among them. The names come from the envelope's table of
        contents, read over the network without downloading it, so this
        screen costs a few hundred bytes and the network picked is the only
        one fetched. The pick is kept on the entry: every refresh asks for
        that member again, never for the envelope.
        """
        if user_input is None:
            return self.async_show_form(
                step_id="inner_zip",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_INNER_ZIP, default=self._inner_zips[0]
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=[
                                selector.SelectOptionDict(value=name, label=name)
                                for name in self._inner_zips])),
                    },
                ),
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "zips": str(len(self._inner_zips)),
                },
            )
        self._user_inputs[CONF_INNER_ZIP] = user_input[CONF_INNER_ZIP]
        check_data = await self.hass.async_add_executor_job(
            ensure_source_zip, self.hass, DEFAULT_PATH, self._user_inputs)
        if check_data:
            return await self._back_to_source(check_data)
        _LOGGER.debug("UserInputs inner zip: %s", self._user_inputs)
        return await self.async_step_source_rt()

    async def async_step_source_key(self, user_input: dict | None = None) -> FlowResult:
        """Ask for the api key, only when the source needs one."""
        errors: dict[str, str] = {}

        def _show(errors: dict[str, str], previous: dict | None = None) -> FlowResult:
            previous = previous or {}
            return self.async_show_form(
                step_id="source_key",
                data_schema=vol.Schema(_source_key_schema(previous)),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            # a key typed on an earlier pass comes back, hidden in its field
            return _show(errors, self._user_inputs)
        user_input = _typed_key(user_input)
        self._user_inputs.update(user_input)
        check_data = await self.hass.async_add_executor_job(
            ensure_source_zip, self.hass, DEFAULT_PATH, self._user_inputs)
        if check_data:
            if check_data == "zip_holds_zips":
                # an envelope behind a key: its networks are offered here as
                # on the url screen, the key kept for the member's download
                self._inner_zips = self._user_inputs.pop("inner_zips", [])
                return await self.async_step_inner_zip()
            # a wrong url and a wrong key fail the same way, and only the
            # url screen can put both right: the error is shown there, with
            # what was typed, and the key waits behind its toggle
            self._pending_error = check_data
            return await self.async_step_source_url()
        _LOGGER.debug(f"UserInputs Source key: {self._user_inputs}")
        return await self.async_step_source_rt()

    async def async_step_source_rt(self, user_input: dict | None = None) -> FlowResult:
        """Offer the source's realtime feeds right after it is brought in.

        Optional by design: submitting the screen empty just moves on, and
        the feeds can be added or changed later from the datasource entry's
        CONFIGURE button. A source picked again shows what it already has.
        """
        errors: dict[str, str] = {}
        source = datasource_entry(self.hass, self._user_inputs.get(CONF_FILE))
        opts = source.options if source else {}
        if user_input is not None and (errors := rt_url_errors(user_input)):
            # shown again with what was typed, for the rider to correct
            opts = {**opts, **user_input}
        if user_input is None or errors:
            return self.async_show_form(
                step_id="source_rt",
                data_schema=vol.Schema(_source_rt_schema(opts)),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )
        if user_input.pop(CONF_NEEDS_API_KEY, False):
            self._source_rt_inputs = user_input
            return await self.async_step_source_rt_key()
        await self._store_source_rt(user_input, {})
        return await self.async_step_agency()

    async def async_step_source_rt_key(self, user_input: dict | None = None) -> FlowResult:
        """Ask for the realtime api key, only when the source needs one."""
        errors: dict[str, str] = {}
        source = datasource_entry(self.hass, self._user_inputs.get(CONF_FILE))
        opts = source.options if source else {}
        if user_input is None:
            return self.async_show_form(
                step_id="source_rt_key",
                data_schema=vol.Schema(_source_rt_key_schema(opts)),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )
        await self._store_source_rt(self._source_rt_inputs, _typed_key(user_input))
        return await self.async_step_agency()

    async def _store_source_rt(self, url_fields: Mapping[str, Any],
                               key_fields: Mapping[str, Any]) -> None:
        """Put what the realtime screens collected onto the datasource entry."""
        inputs = self._user_inputs
        await async_ensure_datasource_entry(
            self.hass, inputs.get(CONF_FILE),
            url=inputs.get(CONF_URL), api=inputs, inner_zip=inputs.get(CONF_INNER_ZIP))
        source = datasource_entry(self.hass, inputs.get(CONF_FILE))
        if source is None:
            _LOGGER.error("No datasource entry to store the realtime config on: %s",
                          inputs.get(CONF_FILE))
            return
        self.hass.config_entries.async_update_entry(
            source, options=_collect_source_rt_options(
                url_fields, key_fields, previous=source.options))

    async def async_step_source_zip(self, user_input: dict | None = None) -> FlowResult:
        """Use a zip the user already dropped in the gtfs2 folder."""
        errors: dict[str, str] = {}

        async def _show(errors: dict[str, str]) -> FlowResult:
            zipfiles = await get_zipfiles(self.hass, DEFAULT_PATH)
            if not zipfiles:
                return self.async_abort(
                    reason="no_zip_in_folder",
                    description_placeholders={
                        **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                        "folder": self.hass.config.path(DEFAULT_PATH),
                    },
                )
            return self.async_show_form(
                step_id="source_zip",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_FILE): vol.In(zipfiles),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            self._take_pending_error(errors)
            return await _show(errors)
        # built from the zip where it lies, and fetched from it from then on,
        # by its file:// url, as a hosted source is by its own
        user_input[CONF_EXTRACT_FROM] = "zip"
        self._source_step = "source_zip"
        user_input[CONF_URL] = source_zip_url(self.hass, user_input[CONF_FILE])
        check_data = await self.hass.async_add_executor_job(
            ensure_source_zip, self.hass, DEFAULT_PATH, user_input)
        if check_data:
            if check_data == "zip_holds_zips":
                # the source is an envelope of networks: which one it
                # follows is asked before anything is downloaded
                self._inner_zips = user_input.pop("inner_zips", [])
                self._user_inputs.update(user_input)
                return await self.async_step_inner_zip()
            errors["base"] = check_data
            return await _show(errors)
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Source zip: {self._user_inputs}")
        return await self.async_step_source_rt()

    async def _fresh_source_of(self, file: str) -> bool:
        """Whether a source has no database yet, or no more: see _fresh_source."""
        return not await self.hass.async_add_executor_job(
            os.path.exists, real_path(self.hass.config.path(DEFAULT_PATH), file))

    async def _fresh_source(self) -> bool:
        """Whether the source picked in this flow has no database yet.

        A fresh source is served from its zip: operators and lines are read
        from the feed, the user chooses, and only the chosen lines are ever
        imported. The full network never has to be unpacked, which on a
        national feed is what makes the difference between a flow that
        continues and one that parks the user behind a progress screen.
        """
        return await self._fresh_source_of(self._user_inputs[CONF_FILE])

    def _take_pending_error(self, errors: dict[str, str]) -> bool:
        """Show, once, the error another screen left for this one: whether
        there was one."""
        if not self._pending_error:
            return False
        errors["base"], self._pending_error = self._pending_error, None
        return True

    async def _back_to_source(self, reason: str) -> FlowResult:
        """Return to the step that picked the datasource, carrying the error.

        The screen is the one the flow remembers picking the source. Worked
        out from the inputs alone, it could not be the list of existing
        sources: that screen stores the same url and extract_from as the
        zip screen, so a source picked there was sent to the zip screen.
        """
        self._pending_error = reason
        if self._source_step == "source_url":
            return await self.async_step_source_url()
        if self._source_step == "source_zip":
            return await self.async_step_source_zip()
        return await self.async_step_start_end()
