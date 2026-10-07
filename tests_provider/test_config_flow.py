"""The config flow, walked screen by screen the way Home Assistant walks it.

A walk starts where a rider starts, at the menu, and answers every screen
through the screen's own schema, as the frontend does: a key the screen
does not ask for, or a value its selector does not offer, is refused
before the step sees it. Home Assistant's side is tests/ha_stub.py (the
flow bases and the selectors) and the manager below, which runs an answer
through the schema, calls the step a menu names, asks a progress screen
again when its task ends, files the entry a flow creates and runs the
executor jobs on threads. The rest is the integration's own: the queries,
the imports, the files, the downloads (the web is answered at requests'
transport adapter) and the entries.

The sources are provider fixtures. An installed one is the fixture's zip
beside the database pygtfs builds from it (fixture_db); a new one is the
zip alone, dropped in the folder or downloaded, and the flow imports what
the rider picks. A pick is made by its place in the list, the second one
where there is a second, so a walk does not only read the head of a list.
The promises:

    bus journey     menu, source, line, departure, way out, arrival, name,
                    closing menu; the entry holds what the sensor reads
                    (file, line and its type, both stops as offered, the
                    name, no direction, no rotation) and nothing of the
                    screens' own switches; another journey on the same line
                    starts from the departure screen
    return          the way back is created beside the outward journey, the
                    stops swapped, under the name the screen announced
    loop            at a loop's terminus the entry keeps the rotation the
                    rider picked, each way its own; anywhere else, none
    train           stations by name, the line's code, route "train"; the
                    return beside it; the same line again; finish
    no arrival      every station of K8+ leads to arrivals; a typed station
                    no train leaves sends the rider back, the name kept
    optimise        what no entry reads is dropped (on Windows the swap is
                    refused and the file stays whole, see the test)
    new source      user_empty, source, source_zip, source_rt: the source's
                    entry; the lines read from the zip; route_reload offers
                    the others, imported along; a line left out is flagged
                    on the next pass and imported alone (route_reload_only)
    import fails    a feed pygtfs cannot read: back on the lines with the
                    reason, no database left, a notification
    import stops    a line fails part way: the lines before it are in, the
                    departure screen and the notification name the others
    no destination  a stop no trip rides on from, and a line the feed names
    no stops        and never runs; the operator screen, from the zip and
                    from the database
    no line         a feed with no line has nothing to offer
    refusals        a typed line not listed; a value not offered, a key not
                    asked: refused, the screen still there to answer
    names           a name in use, for the journey and for its return; a
                    journey the second flow refuses is not announced
    url             a name and an address refused, a name in use, an
                    address answering 404; the download and its record; the
                    realtime feeds and their key on the source's entry
    key             the static key: wrong, then the mask, then right, sent
                    in the query and kept beside the address
    envelope        a zip of networks: which one, and only that one kept
    unpacking       a source still unpacking ends the flow, and is watched
    extracting      the flow waits for an unpacking and goes on with what
                    was typed (local stops)
    local stops     a person or a zone, once per source, a name once
    remove          the files and the source's entry go, the journeys stay
    zip folder      no zip there; a zip taken away while the screen was open
    empty source    a database with no feed and no zip: back to the list
    options         a journey's refresh and offset, its realtime kept; a
                    local stops radius refused over the stop limit; a
                    source's realtime feeds and their key, its static
                    refresh and its key
    words           every screen, error, abort, progress and menu entry a
                    walk meets has its words in strings.json

Every step of the flow is walked. A known defect is marked xfail, strict,
so a fix has to lift the mark.

What no walk reaches, and why:

    no_stops_read    the stop, way and arrival queries raising: every
                     database the flow can open answers them
    generic_failure  from remove, a file deletion refused; from optimise
                     on Linux, a copy that fails (reached on Windows, see
                     optimise)

zip_holds_zips is no error and no abort: a zip of zips is offered its
networks before anything could say so, and its words went with them.

    pytest tests_provider/test_config_flow.py
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import shutil
import sqlite3
import sys
import types
import urllib.parse
import uuid
import zipfile
from pathlib import Path

import pytest
import requests
import voluptuous as vol
from requests.structures import CaseInsensitiveDict

import ha_stub

ha_stub.install()

import fixture_db  # noqa: E402
from homeassistant import config_entries, data_entry_flow  # noqa: E402

const = ha_stub.load("const")
config_flow = ha_stub.load("config_flow")
notifications = ha_stub.load("notifications")
reload = ha_stub.load("flow.reload")
source_entries = ha_stub.load("feed.source_entries")
files = ha_stub.load("feed.files")
# the import into a scratch database has a module of its own here, gtfs_db
# in a checkout run with --component that keeps it there
db_build = (ha_stub.load("data.db_build") if (ha_stub.COMPONENT / "data/db_build.py").is_file()
            else files)
source_refresh = ha_stub.load("data.source_refresh")

FIXTURES = Path(__file__).parent / "fixtures"
COMPONENT = Path(config_flow.__file__).parent
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
DOMAIN = const.DOMAIN

_T = data_entry_flow.FlowResultType
FORM, MENU, ABORT, CREATE = _T.FORM, _T.MENU, _T.ABORT, _T.CREATE_ENTRY
PROGRESS, PROGRESS_DONE = _T.SHOW_PROGRESS, _T.SHOW_PROGRESS_DONE
_UNFINISHED = {FORM, MENU, PROGRESS, PROGRESS_DONE, _T.EXTERNAL_STEP, _T.EXTERNAL_STEP_DONE}
_UNSET = object()


def _zip_url(hass, file):
    """The url a source made from a zip in the folder is fetched from, as
    the component names it: the file:// one of its kept zip."""
    return source_refresh.source_zip_url(hass, file)


# --- Home Assistant's side of a flow ---------------------------------------------

class UnknownStep(Exception):
    """A step the flow names but has no method for: Home Assistant refuses
    the whole flow ("doesn't support step ...")."""


class Entry:
    """A config entry as the integration reads one, data and options
    read-only as Home Assistant hands them out."""

    def __init__(self, *, domain, title, data, options, unique_id, version, source):
        self.entry_id = uuid.uuid4().hex
        self.domain = domain
        self.title = title
        self.unique_id = unique_id
        self.version = version
        self.minor_version = 1
        self.source = source
        self.data = types.MappingProxyType(dict(data))
        self.options = types.MappingProxyType(dict(options))


class FlowManager:
    """hass.config_entries.flow: data_entry_flow.FlowManager's configure
    loop, as Home Assistant 2026.2 runs it. A submission goes through the
    screen's data_schema first, so a key or a value the screen does not
    take is refused before any step sees it (vol.Invalid, which Home
    Assistant wraps in InvalidData); a menu pick runs the step it names; a
    progress screen is asked again when its task ends; a flow that ends,
    on an entry or an abort, is removed and told so (async_remove)."""

    def __init__(self, hass, registry):
        self.hass = hass
        self.registry = registry
        self._progress = {}
        self._progress_tasks = {}
        self._reconfigured = {}
        # every step called and every screen it returned, in order
        self.walked = []
        self.trail = []

    async def _create_flow(self, handler, context):
        flow = config_entries.HANDLERS[handler]()
        flow.init_step = context["source"]
        return flow

    async def _finish(self, flow, result):
        if result["type"] != CREATE:
            return result
        entry = Entry(domain=flow.handler, title=result["title"], data=result["data"],
                      options=result["options"], unique_id=flow.unique_id,
                      version=result["version"], source=flow.context["source"])
        self.registry.entries.append(entry)
        result["result"] = entry
        return result

    async def async_init(self, handler, *, context=None, data=None):
        context = {} if context is None else context
        flow = await self._create_flow(handler, context)
        flow.hass = self.hass
        flow.handler = handler
        flow.flow_id = uuid.uuid4().hex
        flow.context = context
        flow.init_data = data
        self._progress[flow.flow_id] = flow
        return await self._handle_step(flow, flow.init_step, data)

    async def async_configure(self, flow_id, user_input=None):
        result = None
        while not result or result["type"] == PROGRESS_DONE:
            result = await self._async_configure(flow_id, user_input)
        return result

    async def _async_configure(self, flow_id, user_input=None):
        flow = self._progress[flow_id]
        current = flow.cur_step
        if current.get("data_schema") is not None and user_input is not None:
            user_input = current["data_schema"](user_input)
        if current["type"] == MENU and user_input:
            return await self._handle_step(flow, user_input["next_step_id"], None)
        result = await self._handle_step(flow, current["step_id"], user_input)
        if current["type"] == PROGRESS and result["type"] not in (PROGRESS, PROGRESS_DONE):
            raise ValueError("Show progress can only transition to show progress or "
                             "show progress done.")
        return result

    async def _handle_step(self, flow, step_id, user_input):
        if not hasattr(flow, f"async_step_{step_id}"):
            self._remove(flow)
            raise UnknownStep(f"Handler {type(flow).__name__} doesn't support step {step_id}")
        self.walked.append(step_id)
        try:
            result = await getattr(flow, f"async_step_{step_id}")(user_input)
        except data_entry_flow.AbortFlow as err:
            result = flow._result(type=ABORT, reason=err.reason,
                                  description_placeholders=err.description_placeholders)
        if result["type"] == PROGRESS:
            task = result.pop("progress_task", None)
            if task is not None and task is not self._progress_tasks.get(flow.flow_id):
                self._when_done_configure(flow, task)
        else:
            previous = self._progress_tasks.pop(flow.flow_id, None)
            if previous is not None and not previous.done():
                previous.cancel()
        if result["type"] in (FORM, MENU, PROGRESS, _T.EXTERNAL_STEP) and "step_id" not in result:
            result["step_id"] = step_id
        self.trail.append(result)
        if result["type"] in _UNFINISHED:
            if not hasattr(flow, f"async_step_{result['step_id']}"):
                self._remove(flow)
                raise UnknownStep(
                    f"Handler {type(flow).__name__} doesn't support step {result['step_id']}")
            flow.cur_step = result
            return result
        result = await self._finish(flow, result)
        self._remove(flow)
        return result

    def _when_done_configure(self, flow, task):
        """Home Assistant asks the step again once its progress task ends."""
        self._progress_tasks[flow.flow_id] = task
        configured = asyncio.get_running_loop().create_future()
        self._reconfigured[flow.flow_id] = configured

        async def configure():
            try:
                if flow.flow_id in self._progress:
                    await self._async_configure(flow.flow_id)
            except BaseException as err:  # the walk hears it, as HA's log would
                if not configured.done():
                    configured.set_exception(err)
                return
            if not configured.done():
                configured.set_result(None)

        task.add_done_callback(lambda _: self.hass.async_create_task(configure()))

    async def async_finish_progress(self, flow_id):
        """What the frontend does: show the progress screen until the flow
        says it is done, then ask for the screen that follows."""
        while (flow := self._progress.get(flow_id)) is not None \
                and flow.cur_step["type"] == PROGRESS:
            await self._reconfigured[flow_id]
        return await self.async_configure(flow_id)

    def async_progress_by_handler(self, handler, include_uninitialized=False,
                                  match_context=None):
        return [{"flow_id": flow.flow_id, "handler": flow.handler, "context": flow.context,
                 "step_id": (flow.cur_step or {}).get("step_id")}
                for flow in self._progress.values()
                if flow.handler == handler
                and (include_uninitialized or flow.cur_step is not None)
                and all(flow.context.get(k) == v for k, v in (match_context or {}).items())]

    def async_abort(self, flow_id):
        """The rider closes the window."""
        if flow_id in self._progress:
            self._remove(self._progress[flow_id])

    def _remove(self, flow):
        self._progress.pop(flow.flow_id, None)
        task = self._progress_tasks.pop(flow.flow_id, None)
        if task is not None and not task.done():
            task.cancel()
        flow.async_remove()


class OptionsFlowManager(FlowManager):
    """hass.config_entries.options: the flow of one entry, whose options
    its result replaces."""

    async def _create_flow(self, handler, context):
        entry = self.registry.async_get_known_entry(handler)
        flow = config_entries.HANDLERS[entry.domain].async_get_options_flow(entry)
        flow.init_step = "init"
        return flow

    async def _finish(self, flow, result):
        if result["type"] != CREATE:
            return result
        entry = self.registry.async_get_known_entry(flow.handler)
        if result["data"] is not None:
            self.registry.async_update_entry(entry, options=result["data"])
        result["result"] = True
        return result


class Registry:
    """hass.config_entries."""

    def __init__(self, hass):
        self.entries = []
        self.flow = FlowManager(hass, self)
        self.options = OptionsFlowManager(hass, self)

    def async_entries(self, domain=None, include_ignore=True, include_disabled=True):
        return [e for e in self.entries if domain is None or e.domain == domain]

    def async_get_entry(self, entry_id):
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    def async_get_known_entry(self, entry_id):
        entry = self.async_get_entry(entry_id)
        if entry is None:
            raise KeyError(f"no entry {entry_id}")
        return entry

    def async_entry_for_domain_unique_id(self, domain, unique_id):
        return next((e for e in self.entries
                     if e.domain == domain and e.unique_id == unique_id), None)

    def async_update_entry(self, entry, *, data=_UNSET, options=_UNSET, title=_UNSET,
                           unique_id=_UNSET, version=_UNSET, minor_version=_UNSET):
        changed = False
        for name, value in (("data", data), ("options", options), ("title", title),
                            ("unique_id", unique_id), ("version", version),
                            ("minor_version", minor_version)):
            if value is _UNSET:
                continue
            if name in ("data", "options"):
                value = types.MappingProxyType(dict(value))
            if getattr(entry, name) != value:
                setattr(entry, name, value)
                changed = True
        return changed

    async def async_remove(self, entry_id):
        self.entries = [e for e in self.entries if e.entry_id != entry_id]
        return {"require_restart": False}


class States:
    def __init__(self):
        self._states = {}

    def get(self, entity_id):
        return self._states.get(entity_id)

    def async_set(self, entity_id, state, attributes=None):
        self._states[entity_id] = types.SimpleNamespace(
            entity_id=entity_id, state=state, attributes=dict(attributes or {}))


class Hass:
    """The hass a flow is handed: a config folder, the entries, the states,
    an executor that runs a job on a thread as Home Assistant's does, and
    tasks started eagerly, as async_create_task starts them."""

    def __init__(self, config_dir):
        config_dir = str(config_dir)
        self.config = types.SimpleNamespace(
            config_dir=config_dir, path=lambda *parts: os.path.join(config_dir, *parts),
            time_zone="Europe/Paris", language="en")
        self.data = {}
        self.states = States()
        self.config_entries = Registry(self)
        self.tasks = []
        self.background_tasks = []
        self.notifications = []

    def async_add_executor_job(self, target, *args):
        return asyncio.get_running_loop().run_in_executor(None, target, *args)

    def async_create_task(self, target, name=None, eager_start=True):
        task = asyncio.Task(target, loop=asyncio.get_running_loop(), name=name,
                            eager_start=eager_start)
        self.tasks.append(task)
        return task

    def async_create_background_task(self, target, name, eager_start=True):
        task = self.async_create_task(target, name, eager_start)
        self.background_tasks.append(task)
        return task

    def journeys(self):
        return [e for e in self.config_entries.async_entries(DOMAIN)
                if e.data.get(const.CONF_KIND) != const.ENTRY_KIND_DATASOURCE]

    def datasource(self, file):
        return source_entries.datasource_entry(self, file)


# --- the web, as the flow reaches it ----------------------------------------------

class Host:
    """Every url the flow may fetch, answered at requests' transport adapter:
    the query string, the headers and the session the integration builds
    all go through requests itself. A url may want a key, in the query or
    in a header; Range is honoured, as the envelope reader asks for it."""

    def __init__(self):
        self.files = {}
        self.requests = []

    def serve(self, url, body, key=None):
        self.files[url] = (body, key)

    def send(self, adapter, request, **kwargs):
        self.requests.append(request)
        parts = urllib.parse.urlsplit(request.url)
        query = urllib.parse.parse_qs(parts.query)
        body, key = self.files.get(
            urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
            (None, None))
        status, headers = 200, {"Content-Type": "application/zip"}
        if body is None:
            status, body = 404, b"not found"
        elif key and query.get(key[0]) != [key[1]] and request.headers.get(key[0]) != key[1]:
            status, body = 403, b"forbidden"
        elif request.headers.get("Range"):
            start, _, end = request.headers["Range"].removeprefix("bytes=").partition("-")
            if not start:
                start, end = max(len(body) - int(end), 0), len(body) - 1
            start, end = int(start), min(int(end) if end else len(body) - 1, len(body) - 1)
            headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
            status, body = 206, body[start:end + 1]
        response = requests.Response()
        response.status_code = status
        response.reason = {200: "OK", 206: "Partial Content", 403: "Forbidden",
                           404: "Not Found"}[status]
        response.headers = CaseInsensitiveDict({**headers, "Content-Length": str(len(body))})
        response._content = body
        response._content_consumed = True
        response.url = request.url
        response.request = request
        response.connection = adapter
        return response


# --- reading and answering a screen ------------------------------------------------

def fields(result):
    """{key: validator} of a screen's schema."""
    schema = result.get("data_schema")
    return {getattr(marker, "schema", marker): validator
            for marker, validator in (schema.schema.items() if schema is not None else ())}


def offered(result, key):
    """The values a field offers, in the order shown."""
    validator = fields(result)[key]
    if isinstance(validator, vol.In):
        return list(validator.container)
    return [option if isinstance(option, str) else option["value"]
            for option in validator.config["options"]]


def labels(result, key):
    """{value: label} of a select field."""
    validator = fields(result)[key]
    if isinstance(validator, vol.In):
        container = validator.container
        return dict(container) if isinstance(container, dict) else {v: v for v in container}
    return {o["value"]: o["label"] for o in validator.config["options"]}


def default(result, key):
    for marker in result["data_schema"].schema:
        if getattr(marker, "schema", marker) == key:
            value = marker.default
            return value() if callable(value) else value
    raise KeyError(key)


def shown(result, kind, step_id=None):
    """The result is this screen, or the assertion says what came instead."""
    what = (result.get("step_id"), result.get("errors"), result.get("reason"))
    assert result["type"] == kind, f"expected {kind} {step_id}, got {result['type']} {what}"
    if step_id is not None:
        assert result.get("step_id") == step_id, f"expected {step_id}, got {what}"
    return result


async def submit(hass, result, _manager=None, **user_input):
    """Answer a screen, and wait through a progress screen as the rider does."""
    flows = _manager or hass.config_entries.flow
    result = await flows.async_configure(result["flow_id"], user_input)
    if result["type"] == PROGRESS:
        result = await flows.async_finish_progress(result["flow_id"])
    return result


async def choose(hass, result, step, _manager=None):
    return await submit(hass, result, _manager, next_step_id=step)


async def start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


async def options_of(hass, entry):
    return await hass.config_entries.options.async_init(entry.entry_id)


def unworded(hass):
    """What a walk showed that strings.json has no words for: a step, an
    error, an abort, a progress or a menu entry the rider would read as a
    bare key."""
    missing = []
    for section, manager in (("config", hass.config_entries.flow),
                             ("options", hass.config_entries.options)):
        words = STRINGS.get(section, {})
        for result in manager.trail:
            kind, step = result["type"], result.get("step_id")
            if kind in (FORM, MENU) and step not in words.get("step", {}):
                missing.append(f"{section}.step.{step}")
            for error in (result.get("errors") or {}).values():
                if error not in words.get("error", {}):
                    missing.append(f"{section}.error.{error}")
            if kind == ABORT and result["reason"] not in words.get("abort", {}):
                missing.append(f"{section}.abort.{result['reason']}")
            if kind == PROGRESS and result["progress_action"] not in words.get("progress", {}):
                missing.append(f"{section}.progress.{result['progress_action']}")
            if kind == MENU:
                menu = words.get("step", {}).get(step, {}).get("menu_options", {})
                missing += [f"{section}.step.{step}.menu_options.{o}"
                            for o in result["menu_options"] if o not in menu]
    return sorted(set(missing))


# --- a config folder and what sits in it ------------------------------------------

def gtfs_dir(hass):
    folder = Path(hass.config.path(const.DEFAULT_PATH))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def drop_zip(hass, fixture, name):
    """A feed zip dropped in the gtfs2 folder, as a user drops one."""
    shutil.copyfile(FIXTURES / fixture / "static.zip", gtfs_dir(hass) / f"{name}.zip")


async def install_source(hass, fixture, name):
    """A source as an install leaves it: the zip, the database pygtfs built
    from it, and the source's entry."""
    drop_zip(hass, fixture, name)
    schedule = fixture_db.shared(str(FIXTURES / fixture))
    built = sqlite3.connect(schedule.engine.url.database)
    copy = sqlite3.connect(gtfs_dir(hass) / f"{name}.sqlite")
    try:
        built.backup(copy)
    finally:
        copy.close()
        built.close()
    await source_entries.async_ensure_datasource_entry(
        hass, name, api={})


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A config folder, a hass on it, the web, and the words of strings.json.

    The flow words a few labels itself (all operators, line modes, train or
    coach) and the import notifications through Home Assistant's
    translation helper: it answers from strings.json, as it would for an
    English install. A notification raised is kept on hass.notifications."""
    hass = Hass(tmp_path / "config")
    host = Host()
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda adapter, request, **kw: host.send(adapter, request, **kw))
    common = {f"component.{DOMAIN}.common.{key}": text
              for key, text in STRINGS.get("common", {}).items()}

    async def translations(hass, language, category, integrations=None):
        return common if category == "common" else {}

    monkeypatch.setattr(notifications, "async_get_translations", translations)
    monkeypatch.setattr(notifications, "persistent_notification", types.SimpleNamespace(
        async_create=lambda h, message, title=None, notification_id=None:
            h.notifications.append((notification_id, title, message)),
        async_dismiss=lambda h, notification_id: None))
    return types.SimpleNamespace(hass=hass, host=host)


def walk(world, scenario, words=True):
    """Run a walk on a loop of its own, then check its words.

    The flows it leaves open are closed as when the rider closes the
    window, which lets their database go; every screen, error, abort and
    menu entry it met must have its words in strings.json, unless the test
    checks them itself."""
    hass = world.hass

    async def whole():
        try:
            await scenario(hass)
        finally:
            for manager in (hass.config_entries.flow, hass.config_entries.options):
                for flow_id in list(manager._progress):
                    manager.async_abort(flow_id)
    asyncio.run(whole())
    if words:
        assert unworded(hass) == []


def stop_label(result, key, value):
    return labels(result, key)[value]


def rows(hass, file, sql, **params):
    """Ask the database of a source directly, as a check reads it."""
    conn = sqlite3.connect(gtfs_dir(hass) / f"{file}.sqlite")
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def zip_routes(fixture):
    """The route_ids a fixture's manifest says its zip keeps."""
    manifest = json.loads((FIXTURES / fixture / "manifest.json").read_text(encoding="utf-8"))
    return sorted(r for ids in manifest["routes_kept"].values() for r in ids)


def zip_bytes(fixture):
    return (FIXTURES / fixture / "static.zip").read_bytes()


def rewritten_zip(fixture, path, change):
    """The fixture's zip with some members rewritten: change(name, text)
    answers the new text, or None to leave the member as it is."""
    with zipfile.ZipFile(FIXTURES / fixture / "static.zip") as zin, \
            zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            data = zin.read(name)
            new = change(name, data.decode("utf-8-sig"))
            zout.writestr(name, data if new is None else new.encode("utf-8"))


async def to_lines(hass, file):
    """From the menu to the line screen of a source already built."""
    menu = shown(await start(hass), MENU, "user")
    sources = shown(await choose(hass, menu, "start_end"), FORM, "start_end")
    return await submit(hass, sources, file=file)


async def through_stops(hass, result, **answers):
    """Past the stops screen of a bus journey when it is shown: nothing
    ticked, but for the answers given."""
    if result.get("step_id") == "options_stops":
        result = await submit(hass, result, **answers)
    return result


async def bus_journey(hass, file, *, route=1, origin=1, way=1, destination=1,
                      add_return=False, name=None):
    """Menu to closing menu on a bus line, each pick by its place in the list."""
    lines = shown(await to_lines(hass, file), FORM, "route")
    stops = shown(await submit(hass, lines, route=offered(lines, "route")[route]), FORM, "stops")
    result = await submit(hass, stops, origin=offered(stops, "origin")[origin])
    if result["step_id"] == "towards":
        result = await submit(hass, result, towards=offered(result, "towards")[way])
    arrivals = shown(result, FORM, "destination")
    naming = shown(await through_stops(hass, await submit(
        hass, arrivals, destination=offered(arrivals, "destination")[destination])), FORM, "sensor")
    # the return is asked only when there is one to make
    asked = {"add_return": add_return} if "add_return" in fields(naming) else {}
    return await submit(hass, naming, name=name or default(naming, "name"), **asked)


# --- the promises ---------------------------------------------------------------------

def test_a_source_whose_database_is_gone_is_still_offered(world):
    # the source is its entry: a database lost (an error, a restore) is
    # built again, and the source stays on every screen meanwhile. Listed
    # by its .sqlite, it vanished from the menu, the journey screen and the
    # removal, its settings reachable from its entry only
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        (gtfs_dir(hass) / "tao.sqlite").unlink()
        menu = shown(await start(hass), MENU, "user")
        sources = shown(await choose(hass, menu, "start_end"), FORM, "start_end")
        assert offered(sources, "file") == ["tao"]
        # read from the kept zip, as a source just added: the lines chosen
        # are imported, which builds the database again
        lines = shown(await submit(hass, sources, file="tao"), FORM, "route")
        assert all(route.endswith("##pruned") for route in offered(lines, "route"))
        # none is loaded, whatever the feed counts
        assert lines["description_placeholders"]["routes"] == "0"
        removal = shown(await choose(hass, await start(hass), "remove"), FORM, "remove")
        assert offered(removal, "file") == ["tao"]
    walk(world, scenario)


def test_a_source_with_neither_database_nor_zip_says_to_refresh_it(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        (gtfs_dir(hass) / "tao.sqlite").unlink()
        (gtfs_dir(hass) / "tao.zip").unlink()
        sources = shown(await choose(hass, await start(hass), "start_end"), FORM, "start_end")
        again = shown(await submit(hass, sources, file="tao"), FORM, "start_end")
        assert again["errors"] == {"base": "not_built"}
    walk(world, scenario)


def test_a_bus_journey_holds_what_the_sensor_reads(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        menu = shown(await start(hass), MENU, "user")
        assert menu["menu_options"] == ["source", "start_end", "local_stops", "source_real_time",
                                        "source_static_refresh", "remove"]
        sources = shown(await choose(hass, menu, "start_end"), FORM, "start_end")
        assert offered(sources, "file") == ["tao"]
        # one operator in the feed: its screen is not shown
        lines = shown(await submit(hass, sources, file="tao"), FORM, "route")
        routes = offered(lines, "route")
        assert len(routes) == 6 and lines["description_placeholders"]["routes"] == "6"
        route_type, route_id, label = routes[1].split("##")[:3]
        stops = shown(await submit(hass, lines, route=routes[1]), FORM, "stops")
        assert stops["description_placeholders"]["route"] == label
        origin = offered(stops, "origin")[1]
        ways = shown(await submit(hass, stops, origin=origin), FORM, "towards")
        arrivals = shown(await submit(hass, ways, towards=offered(ways, "towards")[1]),
                         FORM, "destination")
        destination = offered(arrivals, "destination")[1]
        naming = shown(await through_stops(hass, await submit(hass, arrivals, destination=destination)),
                       FORM, "sensor")
        name = default(naming, "name")
        assert name == (f"tao {label.split(' : ')[0]} {stop_label(stops, 'origin', origin)}"
                        f" → {stop_label(arrivals, 'destination', destination)}")
        closing = shown(await submit(hass, naming, name=name, add_return=False), MENU, "finished")
        assert closing["menu_options"] == ["same_line", "start_end", "optimise", "finish"]
        [entry] = hass.journeys()
        # what was answered, and nothing of the screens' own switches
        assert dict(entry.data) == {
            "file": "tao", "agency": "0: ALL",
            "route_type": route_type, "route": route_id, "direction": None,
            "origin": origin, "destination": destination, "loop_direction": None,
            "name": name}
        assert (entry.title, entry.unique_id, entry.version) == (name, f"gtfs-{name}", 10)
        assert dict(entry.options) == {}
        again = shown(await choose(hass, closing, "same_line"), FORM, "stops")
        assert again["description_placeholders"]["route"] == label
    walk(world, scenario)


def test_a_bus_journey_gets_on_or_off_at_more_stops(world):
    # a second stop nearer home, as a train journey boards at Les Aubrais
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        lines = await to_lines(hass, "tao")
        stops = shown(await submit(hass, lines, route=offered(lines, "route")[1]), FORM, "stops")
        result = await submit(hass, stops, origin=offered(stops, "origin")[1])
        if result["step_id"] == "towards":
            result = await submit(hass, result, towards=offered(result, "towards")[1])
        arrivals = shown(result, FORM, "destination")
        origin = offered(stops, "origin")[1]
        destination = offered(arrivals, "destination")[-1]
        options = shown(await submit(hass, arrivals, destination=destination), FORM, "options_stops")
        also = offered(options, "also_at")
        assert also and default(options, "also_at") == []
        naming = shown(await submit(hass, options, also_at=[also[0]]), FORM, "sensor")
        shown(await submit(hass, naming, name="to work", add_return=True), MENU, "finished")
        outward, back = hass.journeys()
        # a stop on the way is got on or off at: at both ends, out and back
        assert outward.data["origin_stations"] == [origin, also[0]]
        assert outward.data["destination_stations"] == [destination, also[0]]
        assert back.data["origin_stations"] == [destination, also[0]]
        assert back.data["destination_stations"] == [origin, also[0]]
    walk(world, scenario)


def test_a_bus_journey_s_options_get_on_or_off_at_more_stops(world):
    # the stops screen of the creation, again on the options of a journey
    # made long before
    async def scenario(hass):
        options = hass.config_entries.options
        await install_source(hass, "tao-journeys", "tao")
        lines = await to_lines(hass, "tao")
        stops = shown(await submit(hass, lines, route=offered(lines, "route")[1]), FORM, "stops")
        result = await submit(hass, stops, origin=offered(stops, "origin")[1])
        if result["step_id"] == "towards":
            result = await submit(hass, result, towards=offered(result, "towards")[1])
        arrivals = shown(result, FORM, "destination")
        on_screen = shown(await submit(hass, arrivals, destination=offered(arrivals, "destination")[-1]),
                          FORM, "options_stops")
        naming = shown(await submit(hass, on_screen), FORM, "sensor")
        shown(await submit(hass, naming, name="to work", add_return=False), MENU, "finished")
        (entry,) = hass.journeys()
        assert "origin_stations" not in entry.data
        form = shown(await options_of(hass, entry), FORM, "init")
        also = offered(form, "also_at")
        assert also == offered(on_screen, "also_at") and default(form, "also_at") == []
        # a stop ticked is got on or off at: kept at both ends
        shown(await submit(hass, form, options, also_at=[also[0]]), CREATE)
        assert entry.data["origin_stations"] == [entry.data["origin"], also[0]]
        assert entry.data["destination_stations"] == [entry.data["destination"], also[0]]
        # ticked again on the next opening, by its id
        form = shown(await options_of(hass, entry), FORM, "init")
        assert default(form, "also_at") == [also[0]]
        # an entry made when the screen asked the two apart: the stops of
        # either end ticked
        hass.config_entries.async_update_entry(entry, data={
            **entry.data, "origin_stations": [entry.data["origin"], also[0]],
            "destination_stations": [entry.data["destination"]]})
        form = shown(await options_of(hass, entry), FORM, "init")
        assert default(form, "also_at") == [also[0]]
        # none ticked: the entry has the shape it had
        shown(await submit(hass, form, options, also_at=[]), CREATE)
        assert "origin_stations" not in entry.data and "destination_stations" not in entry.data
    walk(world, scenario)


def test_the_return_journey_is_created_beside_the_outward_one(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        lines = await to_lines(hass, "tao")
        stops = await submit(hass, lines, route=offered(lines, "route")[1])
        ways = await submit(hass, stops, origin=offered(stops, "origin")[1])
        arrivals = await submit(hass, ways, towards=offered(ways, "towards")[1])
        naming = shown(await through_stops(hass, await submit(
            hass, arrivals, destination=offered(arrivals, "destination")[1])), FORM, "sensor")
        # offered, and ticked unless the rider says otherwise
        assert default(naming, "add_return") is True
        back = naming["description_placeholders"]["return_trip"]
        shown(await submit(hass, naming, name=default(naming, "name")), MENU, "finished")
        outward, ride_back = hass.journeys()
        assert dict(ride_back.data) == {
            **outward.data, "origin": outward.data["destination"],
            "destination": outward.data["origin"], "name": back}
        assert ride_back.unique_id == f"gtfs-{back}"
    walk(world, scenario)


def test_no_return_is_offered_that_a_journey_already_rides(world):
    # the return of B -> A is A -> B: offered when A -> B was made already,
    # under another name, it was made a second time, its sensor a _2 of
    # the first
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        lines = await to_lines(hass, "tao")
        route = offered(lines, "route")[1]
        stops = await submit(hass, lines, route=route)
        ways = await submit(hass, stops, origin=offered(stops, "origin")[1])
        arrivals = await submit(hass, ways, towards=offered(ways, "towards")[1])
        naming = shown(await through_stops(hass, await submit(
            hass, arrivals, destination=offered(arrivals, "destination")[1])), FORM, "sensor")
        shown(await submit(hass, naming, name="to work", add_return=False), MENU, "finished")
        [outward] = hass.journeys()

        def same_stop(options, entry):
            return next(o for o in options if o.split(": ")[0] == entry.split(": ")[0])

        # the same ride the other way round, under a name of its own
        naming = None
        for way in range(4):
            lines = await to_lines(hass, "tao")
            stops = await submit(hass, lines, route=route)
            result = await submit(hass, stops,
                                  origin=same_stop(offered(stops, "origin"), outward.data["destination"]))
            if result["step_id"] == "towards":
                if way >= len(offered(result, "towards")):
                    break
                result = await submit(hass, result, towards=offered(result, "towards")[way])
            wanted = [o for o in offered(result, "destination")
                      if o.split(": ")[0] == outward.data["origin"].split(": ")[0]]
            if wanted:
                naming = shown(await through_stops(hass, await submit(hass, result, destination=wanted[0])),
                               FORM, "sensor")
                break
        assert naming is not None
        back = shown(await submit(hass, naming, name="back home"), MENU, "finished")
        assert back["step_id"] == "finished"
        assert sorted(e.data["name"] for e in hass.journeys()) == ["back home", "to work"]
    walk(world, scenario)


def test_a_loop_keeps_the_rotation_only_at_its_terminus(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        # line 22 rides from Zenith round to Zenith; its terminus is last in
        # the list, and the way out is asked from every stop of a loop
        rotations = []
        for way in (0, 1):
            shown(await bus_journey(hass, "tao", route=0, origin=-1, way=way, destination=-1),
                  MENU, "finished")
            rotations.append(hass.journeys()[-1].data["loop_direction"])
        assert sorted(rotations) == ["0", "1"]
        # away from the terminus the pair says the rotation: none is kept
        shown(await bus_journey(hass, "tao", route=0, origin=0, way=0, destination=0),
              MENU, "finished")
        assert hass.journeys()[-1].data["loop_direction"] is None
    walk(world, scenario)


async def through_options(hass, result, **answers):
    """Past the train options screen when it is shown: its defaults, but
    for the answers given."""
    if result.get("step_id") == "options_train":
        given = {key: default(result, key) for key in fields(result)}
        result = await submit(hass, result, **{**given, **answers})
    return result


def test_a_train_journey_and_its_return_hold_the_stations_and_the_line(world):
    async def scenario(hass):
        await install_source(hass, "sncf-journeys", "sncf")
        lines = shown(await to_lines(hass, "sncf"), FORM, "route")
        routes = offered(lines, "route")
        # every train line first, then the four lines
        assert len(routes) == 5 and all(r.startswith("2##") for r in routes)
        assert routes[0].split("##")[1] == "train"
        label = routes[2].split("##")[2]
        stations = shown(await submit(hass, lines, route=routes[2]), FORM, "stops_train")
        origin = offered(stations, "origin")[1]
        arrivals = shown(await submit(hass, stations, origin=origin), FORM, "destination_train")
        destination = offered(arrivals, "destination")[1]
        options = await submit(hass, arrivals, destination=destination)
        line = label.split(" : ")[0]
        if options["step_id"] == "options_train":
            # the line picked is ticked, and only it
            assert default(options, "lines") == [line]
        naming = shown(await through_options(hass, options), FORM, "sensor_train")
        assert default(naming, "name") == f"sncf {line} {origin} → {destination}"
        # a train return is offered unticked
        assert default(naming, "add_return") is False
        back = naming["description_placeholders"]["return_trip"]
        assert back == f"sncf {line} {destination} → {origin}"
        closing = shown(await submit(hass, naming, name=default(naming, "name"), add_return=True),
                        MENU, "finished")
        outward, ride_back = hass.journeys()
        assert dict(outward.data) == {
            "file": "sncf", "agency": "0: ALL",
            "route_type": "2", "route": "train", "direction": "0", "line": line,
            "lines": [line], "origin_stations": [origin], "destination_stations": [destination],
            "origin": origin, "destination": destination, "name": default(naming, "name")}
        # the mirror: the stations swap ends, the lines stay
        assert dict(ride_back.data) == {**outward.data, "origin": destination,
                                        "destination": origin, "name": back,
                                        "origin_stations": [destination],
                                        "destination_stations": [origin]}
        # another journey on the same line starts from its departure screen
        again = shown(await choose(hass, closing, "same_line"), FORM, "stops_train")
        assert again["description_placeholders"]["route"] == label
        arrivals = await submit(hass, again, origin=offered(again, "origin")[2])
        options = await submit(hass, arrivals, destination=offered(arrivals, "destination")[0])
        naming = shown(await through_options(hass, options), FORM, "sensor_train")
        closing = shown(await submit(hass, naming, name=default(naming, "name"), add_return=False),
                        MENU, "finished")
        assert len(hass.journeys()) == 3
        assert shown(await choose(hass, closing, "finish"), ABORT)["reason"] == "finished"
    walk(world, scenario)


def test_a_train_journey_from_the_stations_first_makes_a_sensor_a_line(world):
    # a week of works: some trains to Paris leave from Les Aubrais, under
    # whichever code. From the stations first, each line ticked is a sensor
    # of its own, getting on at Les Aubrais as well, and its return getting
    # off there as well
    async def scenario(hass):
        await install_source(hass, "sncf-journeys", "sncf")
        lines = shown(await to_lines(hass, "sncf"), FORM, "route")
        first = offered(lines, "route")[0]
        assert first.split("##")[:2] == ["2", "train"]
        stations = shown(await submit(hass, lines, route=first), FORM, "stops_train")
        assert {"Orléans", "Les Aubrais", "Paris Austerlitz"} <= set(offered(stations, "origin"))
        arrivals = shown(await submit(hass, stations, origin="Orléans"), FORM, "destination_train")
        options = shown(await submit(hass, arrivals, destination="Paris Austerlitz"),
                        FORM, "options_train")
        assert "Les Aubrais" in offered(options, "also_at")
        # every line riding between them ticked; each offered under its name too
        assert set(default(options, "lines")) == {"K8+", "P8"} == set(offered(options, "lines"))
        assert labels(options, "lines")["K8+"] == "K8+ (Paris - Orléans)"
        # a sensor a line: no line ticked is no sensor
        again = shown(await submit(hass, options, also_at=["Les Aubrais"], lines=[]),
                      FORM, "options_train")
        assert again["errors"] == {"base": "no_line_ticked"}
        sensors = shown(await submit(hass, again, also_at=["Les Aubrais"],
                                     lines=["K8+", "P8"]), FORM, "sensors_train")
        # each named from its line and its stations, no name to type
        assert "name" not in fields(sensors)
        placeholders = sensors["description_placeholders"]
        assert placeholders["sensors"] == ("- sncf K8+ Orléans → Paris Austerlitz\n"
                                           "- sncf P8 Orléans → Paris Austerlitz")
        assert placeholders["returns"] == ("- sncf K8+ Paris Austerlitz → Orléans\n"
                                           "- sncf P8 Paris Austerlitz → Orléans")
        # a train return is offered unticked, as on the sensor screen
        assert default(sensors, "add_return") is False
        closing = shown(await submit(hass, sensors, add_return=True), MENU, "finished")
        made = {entry.data["name"]: entry.data for entry in hass.journeys()}
        assert closing["description_placeholders"]["name"] == ", ".join(made)
        assert sorted(made) == ["sncf K8+ Orléans → Paris Austerlitz", "sncf K8+ Paris Austerlitz → Orléans",
                                "sncf P8 Orléans → Paris Austerlitz", "sncf P8 Paris Austerlitz → Orléans"]
        k8 = made["sncf K8+ Orléans → Paris Austerlitz"]
        assert (k8["route"], k8["line"], k8["lines"]) == ("train", "K8+", ["K8+"])
        assert (k8["origin"], k8["destination"]) == ("Orléans", "Paris Austerlitz")
        # Les Aubrais got on or off at, out and back
        assert k8["origin_stations"] == ["Orléans", "Les Aubrais"]
        assert k8["destination_stations"] == ["Paris Austerlitz", "Les Aubrais"]
        back = made["sncf K8+ Paris Austerlitz → Orléans"]
        assert (back["origin"], back["destination"]) == ("Paris Austerlitz", "Orléans")
        assert back["origin_stations"] == ["Paris Austerlitz", "Les Aubrais"]
        assert back["destination_stations"] == ["Orléans", "Les Aubrais"]
        assert (back["line"], back["lines"]) == ("K8+", ["K8+"])
        assert made["sncf P8 Orléans → Paris Austerlitz"]["lines"] == ["P8"]
    walk(world, scenario)


def _p8_never_at_orleans():
    """stop_times.txt with the P8 of Paris - Orleans no longer calling at
    Orleans: a line serving one of the two stations ticked at an end, as
    most K6+ from Tours call at Les Aubrais and not at Orleans."""
    with zipfile.ZipFile(FIXTURES / "sncf-journeys" / "static.zip") as feed:
        def read(name):
            return list(csv.DictReader(io.TextIOWrapper(feed.open(name), "utf-8-sig")))
        routes = {r["route_id"] for r in read("routes.txt")
                  if r["route_long_name"] == "Paris - Etampes - Orléans"}
        trips = {t["trip_id"] for t in read("trips.txt") if t["route_id"] in routes}
        orleans = {s["stop_id"] for s in read("stops.txt") if s["stop_name"] == "Orléans"}

    def change(name, text):
        if name.rsplit("/", 1)[-1] != "stop_times.txt":
            return None
        header, *lines = text.splitlines()
        columns = header.split(",")
        trip, stop = columns.index("trip_id"), columns.index("stop_id")
        kept = [line for line in lines
                if not (line.split(",")[trip] in trips and line.split(",")[stop] in orleans)]
        return "\n".join([header, *kept]) + "\n"
    return change


def test_a_line_ticked_keeps_only_the_stations_it_serves(world, tmp_path):
    # the P8 here calls at Les Aubrais, not at Orleans: its sensor leaves
    # from Les Aubrais, and the K8+ made before is left as it is
    feed = tmp_path / "aubrais"
    feed.mkdir()
    rewritten_zip("sncf-journeys", feed / "static.zip", _p8_never_at_orleans())

    async def scenario(hass):
        shutil.copyfile(feed / "static.zip", gtfs_dir(hass) / "rail.zip")
        built = sqlite3.connect(fixture_db.build(str(feed)).engine.url.database)
        copy = sqlite3.connect(gtfs_dir(hass) / "rail.sqlite")
        try:
            built.backup(copy)
        finally:
            copy.close()
            built.close()
        await source_entries.async_ensure_datasource_entry(hass, "rail", api={})

        async def options():
            lines = shown(await to_lines(hass, "rail"), FORM, "route")
            stations = shown(await submit(hass, lines, route=offered(lines, "route")[0]),
                             FORM, "stops_train")
            arrivals = shown(await submit(hass, stations, origin="Orléans"), FORM, "destination_train")
            return shown(await submit(hass, arrivals, destination="Paris Austerlitz"),
                         FORM, "options_train")

        # the K8+ alone: one line ticked, named on the sensor screen
        naming = shown(await submit(hass, await options(), also_at=["Les Aubrais"],
                                    lines=["K8+"]), FORM, "sensor_train")
        assert default(naming, "name") == "rail K8+ Orléans → Paris Austerlitz"
        shown(await submit(hass, naming, name=default(naming, "name"), add_return=False),
              MENU, "finished")
        sensors = shown(await submit(hass, await options(), also_at=["Les Aubrais"],
                                     lines=["K8+", "P8"]), FORM, "sensors_train")
        assert sensors["description_placeholders"]["sensors"] == (
            "- rail K8+ Orléans → Paris Austerlitz\n- rail P8 Les Aubrais → Paris Austerlitz")
        # the K8+ is there already: only the P8 is made
        closing = shown(await submit(hass, sensors, add_return=False), MENU, "finished")
        assert closing["description_placeholders"]["name"] == "rail P8 Les Aubrais → Paris Austerlitz"
        k8, p8 = (entry.data for entry in hass.journeys())
        assert k8["origin_stations"] == ["Orléans", "Les Aubrais"]
        assert (p8["origin"], p8["origin_stations"]) == ("Les Aubrais", ["Les Aubrais"])
        assert (p8["line"], p8["destination_stations"]) == ("P8", ["Paris Austerlitz"])
    walk(world, scenario)


def held_routes(hass, file):
    """The route ids a source's database holds; none before it has one.
    Looked at without opening it: a connection would make the file."""
    if not (gtfs_dir(hass) / f"{file}.sqlite").exists():
        return set()
    if not rows(hass, file, "select 1 from sqlite_master where type = 'table' and name = 'trips'"):
        return set()
    return {r for (r,) in rows(hass, file, "select distinct route_id from trips")}


async def fresh_rail_options(hass):
    """A fresh source of sncf-journeys, the stations first, Orleans to
    Paris Austerlitz: the options screen."""
    drop_zip(hass, "sncf-journeys", "sncf")
    where = await choose(hass, await start(hass), "source")
    folder = await choose(hass, where, "source_zip")
    # past the realtime screen, left empty
    return await rail_options(hass, shown(await submit(hass, await submit(hass, folder, file="sncf")),
                                          FORM, "route"))


async def rail_options(hass, lines):
    """From the line screen, the stations first, Orleans to Paris
    Austerlitz: the options screen."""
    first = offered(lines, "route")[0]
    assert first.split("##")[:2] == ["2", "train"]
    stations = shown(await submit(hass, lines, route=first), FORM, "stops_train")
    # the feed's stations, Jura and Camargue lines included
    assert {"Orléans", "Paris Austerlitz", "Saint-Claude"} <= set(offered(stations, "origin"))
    arrivals = shown(await submit(hass, stations, origin="Orléans"), FORM, "destination_train")
    return shown(await submit(hass, arrivals, destination="Paris Austerlitz"),
                 FORM, "options_train")


K8 = "FR:Line::1BF2D66F-09EF-4CB8-A003-1417C1EA6532:"
P8_ORLEANS = "FR:Line::89BD9468-3499-4B6B-B3CB-50073CDD3F95:"


def test_a_train_journey_from_the_stations_first_imports_the_lines_it_rides(world):
    # a source holds the lines asked for, not the network: on a fresh one
    # the stations and the lines come from the zip, and the lines ticked
    # are imported once ticked, either way round
    async def scenario(hass):
        options = await fresh_rail_options(hass)
        assert "import_partial" not in (options.get("errors") or {}).values()
        assert {"K8+", "P8"} <= set(offered(options, "lines"))
        sensors = shown(await submit(hass, options, also_at=["Les Aubrais"],
                                     lines=["K8+", "P8"]), FORM, "sensors_train")
        # K8+ and the P8 of Paris - Orleans, not the P8 of the Jura or of Nimes
        assert held_routes(hass, "sncf") == {K8, P8_ORLEANS}
        shown(await submit(hass, sensors, add_return=True), MENU, "finished")
        made = {entry.data["name"]: entry.data for entry in hass.journeys()}
        assert len(made) == 4
        outward = made["sncf K8+ Orléans → Paris Austerlitz"]
        assert outward["origin_stations"] == ["Orléans", "Les Aubrais"]
        assert (outward["lines"], outward["line"]) == (["K8+"], "K8+")
        ride_back = made["sncf P8 Paris Austerlitz → Orléans"]
        assert ride_back["destination_stations"] == ["Orléans", "Les Aubrais"]
        assert (ride_back["lines"], ride_back["line"]) == (["P8"], "P8")
    walk(world, scenario)


def test_a_train_journey_imports_only_the_lines_ticked(world):
    # every line riding between the two stations was imported as the
    # arrival was picked: a flow closed on the options screen kept them all
    # (.239: nineteen lines for Paris Austerlitz -> Bordeaux, none ticked)
    async def scenario(hass):
        options = await fresh_rail_options(hass)
        hass.config_entries.flow.async_abort(options["flow_id"])
        assert held_routes(hass, "sncf") == set()
        options = await rail_options(hass, shown(await to_lines(hass, "sncf"), FORM, "route"))
        naming = shown(await submit(hass, options, also_at=[], lines=["K8+"]),
                       FORM, "sensor_train")
        assert held_routes(hass, "sncf") == {K8}
        shown(await submit(hass, naming, name=default(naming, "name"), add_return=True),
              MENU, "finished")
        assert sorted(entry.data["line"] for entry in hass.journeys()) == ["K8+", "K8+"]
    walk(world, scenario)


def _without_short_names(name, text):
    """routes.txt with every route_short_name left empty, the way Metro-North
    and Amtrak publish their lines: a long name only."""
    if name.rsplit("/", 1)[-1] != "routes.txt":
        return None
    header, *lines = text.splitlines()
    at = header.split(",").index("route_short_name")
    blanked = [",".join("" if i == at else cell for i, cell in enumerate(line.split(",")))
               for line in lines]
    return "\n".join([header, *blanked]) + "\n"


def test_a_train_line_with_no_short_name_leads_somewhere(world, tmp_path):
    # the line code was the label the route screen shows, which falls back
    # to the long name: no route_short_name equals "Dole - St Claude", and
    # every station of such a line led nowhere (sweep 2026-09-26, Metro-North
    # and Amtrak: no train journey could be created at all)
    feed = tmp_path / "nameless"
    feed.mkdir()
    rewritten_zip("sncf-journeys", feed / "static.zip", _without_short_names)

    async def scenario(hass):
        shutil.copyfile(feed / "static.zip", gtfs_dir(hass) / "rail.zip")
        built = sqlite3.connect(fixture_db.build(str(feed)).engine.url.database)
        copy = sqlite3.connect(gtfs_dir(hass) / "rail.sqlite")
        try:
            built.backup(copy)
        finally:
            copy.close()
            built.close()
        await source_entries.async_ensure_datasource_entry(
            hass, "rail", api={})
        lines = shown(await to_lines(hass, "rail"), FORM, "route")
        (route,) = [r for r in offered(lines, "route") if r.split("##")[2].startswith("Dole - St Claude")]
        label = route.split("##")[2]
        stations = shown(await submit(hass, lines, route=route), FORM, "stops_train")
        origin = offered(stations, "origin")[0]
        arrivals = shown(await submit(hass, stations, origin=origin), FORM, "destination_train")
        destination = offered(arrivals, "destination")[0]
        options = await submit(hass, arrivals, destination=destination)
        naming = shown(await through_options(hass, options), FORM, "sensor_train")
        # the label still names the sensor; the entry holds no line code
        assert default(naming, "name") == f"rail {label.split(' : ')[0]} {origin} → {destination}"
        await submit(hass, naming, name=default(naming, "name"), add_return=False)
        (journey,) = hass.journeys()
        assert journey.data["line"] is None
    walk(world, scenario)


def _as_suburban_rail(name, text):
    """routes.txt with every rail route filed under 109, suburban railway,
    the extended type Helsinki and Leipzig give their commuter trains."""
    if name.rsplit("/", 1)[-1] != "routes.txt":
        return None
    header, *lines = text.splitlines()
    at = header.split(",").index("route_type")
    moved = [",".join("109" if i == at and cell == "2" else cell for i, cell in enumerate(line.split(",")))
             for line in lines]
    return "\n".join([header, *moved]) + "\n"


def test_a_line_of_an_extended_rail_type_takes_the_train_screens(world, tmp_path):
    # a 109 line picked alone went through the bus screens (stop by quay,
    # a direction to pick), while the stations first choice took it as a
    # train: one line, two ways to set it up
    feed = tmp_path / "suburban"
    feed.mkdir()
    rewritten_zip("sncf-journeys", feed / "static.zip", _as_suburban_rail)

    async def scenario(hass):
        shutil.copyfile(feed / "static.zip", gtfs_dir(hass) / "rail.zip")
        built = sqlite3.connect(fixture_db.build(str(feed)).engine.url.database)
        copy = sqlite3.connect(gtfs_dir(hass) / "rail.sqlite")
        try:
            built.backup(copy)
        finally:
            copy.close()
            built.close()
        await source_entries.async_ensure_datasource_entry(
            hass, "rail", api={})
        lines = shown(await to_lines(hass, "rail"), FORM, "route")
        (route,) = [r for r in offered(lines, "route") if "Dole - St Claude" in r.split("##")[2]]
        assert route.split("##")[0] == "109"
        stations = shown(await submit(hass, lines, route=route), FORM, "stops_train")
        origin = offered(stations, "origin")[0]
        arrivals = shown(await submit(hass, stations, origin=origin), FORM, "destination_train")
        destination = offered(arrivals, "destination")[0]
        options = await submit(hass, arrivals, destination=destination)
        naming = shown(await through_options(hass, options), FORM, "sensor_train")
        await submit(hass, naming, name=default(naming, "name"), add_return=False)
        (journey,) = hass.journeys()
        assert journey.data["route_type"] == "2"
    walk(world, scenario)


def test_a_station_no_train_leaves_sends_the_rider_back_to_the_departures(world):
    async def scenario(hass):
        await install_source(hass, "sncf-journeys", "sncf")

        async def departures():
            # the first line, past the every-line option heading the list
            lines = await to_lines(hass, "sncf")
            return shown(await submit(hass, lines, route=offered(lines, "route")[1]),
                         FORM, "stops_train")

        stations = offered(await departures(), "origin")
        assert len(stations) == 6
        for station in stations:
            arrivals = await submit(hass, await departures(), origin=station)
            assert offered(shown(arrivals, FORM, "destination_train"), "destination")
        # the field takes a typed name too: one no train leaves is the sure
        # way onto the path with no arrival
        again = shown(await submit(hass, await departures(), origin="Nowhere Central"),
                      FORM, "stops_train")
        assert again["errors"] == {"base": "no_destination"}
        assert default(again, "origin") == "Nowhere Central"
        shown(await submit(hass, again, origin=stations[0]), FORM, "destination_train")
    walk(world, scenario)


def test_optimise_keeps_the_lines_the_entries_read_and_drops_the_rest(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        closing = shown(await bus_journey(hass, "tao"), MENU, "finished")
        [entry] = hass.journeys()
        screen = shown(await choose(hass, closing, "optimise"), FORM, "optimise")
        assert (screen["description_placeholders"]["kept"],
                screen["description_placeholders"]["dropped"]) == ("1", "5")
        done = shown(await submit(hass, screen), ABORT)
        loaded = sorted(r for (r,) in rows(hass, "tao", "select distinct route_id from trips"))
        if sys.platform == "win32":
            # the optimised copy takes the database's place by a rename,
            # which Windows refuses while this process holds the file open,
            # the flow's own schedule among them; Linux, where Home
            # Assistant runs, renames over it. Refused, the file stays whole
            assert done["reason"] == "generic_failure"
            assert loaded == zip_routes("tao-journeys")
        else:
            assert done["reason"] == "optimised"
            assert loaded == [entry.data["route"]]
    walk(world, scenario)


def test_a_new_source_imports_only_the_lines_picked(world, monkeypatch):
    async def scenario(hass):
        drop_zip(hass, "tao-journeys", "tao")
        # nothing built yet: the first menu only leads to a source
        menu = shown(await start(hass), MENU, "user_empty")
        assert menu["menu_options"] == ["source"]
        where = shown(await choose(hass, menu, "source"), MENU, "source")
        assert where["menu_options"] == ["source_url", "source_zip"]
        folder = shown(await choose(hass, where, "source_zip"), FORM, "source_zip")
        assert offered(folder, "file") == ["tao"]
        feeds = shown(await submit(hass, folder, file="tao"), FORM, "source_rt")
        lines = shown(await submit(hass, feeds), FORM, "route")
        source = hass.datasource("tao")
        # fetched from the zip in the folder, by its file:// url
        assert dict(source.data) == {
            "kind": "datasource", "file": "tao",
            "url": _zip_url(hass, "tao"),
            "api_key_location": "not_applicable"}
        assert dict(source.options) == {} and source.unique_id == "gtfs2-source-tao"
        # read from the zip: every line still has its timetable to import
        routes = offered(lines, "route")
        assert all(r.endswith("##pruned") for r in routes)
        assert sorted(r.split("##")[1] for r in routes) == zip_routes("tao-journeys")
        picked = routes[1].split("##")[1]
        also = shown(await submit(hass, lines, route=routes[1]), FORM, "route_reload")
        others = offered(also, "also_reload")
        assert sorted(others) == sorted(set(zip_routes("tao-journeys")) - {picked})
        assert fields(also)["also_reload"].config["mode"] == "list"
        left_out = others[-1]
        stops = shown(await submit(hass, also, also_reload=others[:-1]), FORM, "stops")
        assert "importing" in hass.config_entries.flow.walked
        assert "reload_done" in hass.config_entries.flow.walked
        loaded = {r for (r,) in rows(hass, "tao", "select distinct route_id from trips")}
        assert loaded == set(zip_routes("tao-journeys")) - {left_out}
        # said again outside the flow, for a rider who closed the window
        await asyncio.gather(*hass.background_tasks)
        assert [n[:2] for n in hass.notifications] == [
            ("gtfs2_import_tao", STRINGS["common"]["import_done_title"].format(file="tao"))]
        origin = offered(stops, "origin")[0]
        result = await submit(hass, stops, origin=origin)
        if result["step_id"] == "towards":
            result = await submit(hass, result, towards=offered(result, "towards")[0])
        naming = await submit(hass, result, destination=offered(result, "destination")[0])
        closing = shown(await submit(hass, naming, name="first", add_return=False),
                        MENU, "finished")
        assert hass.journeys()[0].data["route"] == picked
        # the line left out is flagged on the next pass, and alone to import
        sources = shown(await choose(hass, closing, "start_end"), FORM, "start_end")
        assert default(sources, "file") == "tao"
        lines = shown(await submit(hass, sources, file="tao"), FORM, "route")
        flagged = [r for r in offered(lines, "route") if r.endswith("##pruned")]
        assert [r.split("##")[1] for r in flagged] == [left_out]
        alone = shown(await submit(hass, lines, route=flagged[0]), FORM, "route_reload_only")
        assert fields(alone) == {}
        # the route screen opened the database built on the first pass; the
        # reopening after the import lets that one go first, where it stayed
        # open until collected, holding the file on Windows
        [flow] = hass.config_entries.flow._progress.values()
        held = flow._pygtfs
        let_go = []
        close = reload.close_schedule
        monkeypatch.setattr(reload, "close_schedule",
                            lambda schedule: (let_go.append(schedule), close(schedule)))
        result = await submit(hass, alone)
        assert hasattr(held, "session") and held in let_go
        assert result["step_id"] in ("stops", "towards")
        loaded = {r for (r,) in rows(hass, "tao", "select distinct route_id from trips")}
        assert loaded == set(zip_routes("tao-journeys"))
    walk(world, scenario)


def test_a_feed_that_cannot_be_imported_sends_the_rider_back_to_the_lines(world):
    def late(name, text):
        # a time pygtfs cannot read, on every call of every trip
        if name != "stop_times.txt":
            return None
        header, *calls = text.splitlines()
        column = header.split(",").index("arrival_time")
        broken = [",".join("late" if i == column else v for i, v in enumerate(c.split(",")))
                  for c in calls]
        return "\n".join([header, *broken]) + "\n"

    async def scenario(hass):
        rewritten_zip("tao-journeys", gtfs_dir(hass) / "tao.zip", late)
        where = await choose(hass, await start(hass), "source")
        folder = await choose(hass, where, "source_zip")
        lines = shown(await submit(hass, await submit(hass, folder, file="tao")), FORM, "route")
        also = shown(await submit(hass, lines, route=offered(lines, "route")[0]), FORM, "route_reload")
        again = shown(await submit(hass, also, also_reload=[]), FORM, "route")
        assert again["errors"] == {"base": "reload_failed"}
        assert "reload_failed" in hass.config_entries.flow.walked
        assert not (gtfs_dir(hass) / "tao.sqlite").exists()
        await asyncio.gather(*hass.background_tasks)
        assert [n[:2] for n in hass.notifications] == [
            ("gtfs2_import_tao", STRINGS["common"]["import_failed_title"].format(file="tao"))]
    walk(world, scenario)


def test_an_import_that_stops_at_a_line_names_the_lines_left_out(world, monkeypatch):
    async def scenario(hass):
        drop_zip(hass, "tao-journeys", "tao")
        where = await choose(hass, await start(hass), "source")
        folder = await choose(hass, where, "source_zip")
        lines = shown(await submit(hass, await submit(hass, folder, file="tao")), FORM, "route")
        picked = offered(lines, "route")[1]
        also = shown(await submit(hass, lines, route=picked), FORM, "route_reload")
        others = offered(also, "also_reload")
        # the second line asked along fails to copy: the import stops there,
        # and the lines after it are not tried. A new source holding only
        # the lines asked is its scratch file renamed, every line or none
        # (take_scratch_whole): the copy, where an import can stop part
        # way, is what a source with lines already in goes through, and a
        # rename that fails. Taken here.
        monkeypatch.setattr(db_build, "take_scratch_whole", lambda *args: None)
        copy = db_build.copy_route
        monkeypatch.setattr(db_build, "copy_route", lambda real, scratch, route_id, shared=True:
                            None if route_id == others[1] else copy(real, scratch, route_id, shared))
        stops = shown(await submit(hass, also, also_reload=others), FORM, "stops")
        assert "reload_done" in hass.config_entries.flow.walked
        loaded = {r for (r,) in rows(hass, "tao", "select distinct route_id from trips")}
        assert loaded == {picked.split("##")[1], others[0]}
        # the line picked came in, so the flow carries on, and says which
        # did not
        missing = ", ".join(r.split(":")[-1] for r in others[1:])
        assert stops["errors"] == {"base": "import_partial"}
        assert stops["description_placeholders"]["missing"] == missing
        # the notification names them beside the lines that came in
        await asyncio.gather(*hass.background_tasks)
        came_in = ", ".join(r.split(":")[-1] for r in (picked.split("##")[1], others[0]))
        assert hass.notifications == [(
            "gtfs2_import_tao",
            STRINGS["common"]["import_partial_title"].format(file="tao"),
            STRINGS["common"]["import_partial"].format(file="tao", lines=came_in, missing=missing))]
        # said once: another journey on the same line starts from a
        # departure screen with nothing to add
        result = await submit(hass, stops, origin=offered(stops, "origin")[0])
        if result["step_id"] == "towards":
            result = await submit(hass, result, towards=offered(result, "towards")[0])
        naming = await submit(hass, result, destination=offered(result, "destination")[0])
        closing = shown(await submit(hass, naming, name="first", add_return=False),
                        MENU, "finished")
        again = shown(await choose(hass, closing, "same_line"), FORM, "stops")
        assert not again["errors"]
    walk(world, scenario)


def test_a_stop_no_trip_rides_on_from_and_a_line_with_no_trip_end_the_flow(world):
    # a line the feed names and never runs, beside the night line N1 whose
    # last trips leave nobody a destination from Currie St
    def ghost(name, text):
        if name != "routes.txt":
            return None
        header, *routes = text.splitlines()
        return "\n".join([header, *routes, "GHOST,5,GHOST,Never runs,,3,,,,"]) + "\n"

    async def scenario(hass):
        rewritten_zip("adelaide", gtfs_dir(hass) / "adelaide.zip", ghost)
        where = await choose(hass, await start(hass), "source")
        folder = await choose(hass, where, "source_zip")
        operators = shown(await submit(hass, await submit(hass, folder, file="adelaide")),
                          FORM, "agency")
        names = labels(operators, "agency")
        assert names["0: ALL"] == STRINGS["common"]["agency_all"]
        torrens = next(a for a in names if a.startswith("5: "))
        lines = shown(await submit(hass, operators, agency=torrens), FORM, "route")
        routes = {r.split("##")[1]: r for r in offered(lines, "route")}
        assert sorted(routes) == ["GHOST", "N1"]
        also = shown(await submit(hass, lines, route=routes["N1"]), FORM, "route_reload")
        # the other lines on offer are the operator's
        assert offered(also, "also_reload") == ["GHOST"]
        stops = shown(await submit(hass, also, also_reload=[]), FORM, "stops")
        currie = next(s for s, label in labels(stops, "origin").items()
                      if label == "Stop W1 Currie St - South side")
        assert shown(await submit(hass, stops, origin=currie), ABORT)["reason"] == "no_destination"

        operators = shown(await to_lines(hass, "adelaide"), FORM, "agency")
        lines = shown(await submit(hass, operators, agency=torrens), FORM, "route")
        [flagged] = [r for r in offered(lines, "route") if r.endswith("##pruned")]
        assert flagged.split("##")[1] == "GHOST"
        alone = shown(await submit(hass, lines, route=flagged), FORM, "route_reload_only")
        assert shown(await submit(hass, alone), ABORT)["reason"] == "no_stops"
    walk(world, scenario)


def test_a_feed_with_no_line_has_nothing_to_offer(world):
    async def scenario(hass):
        rewritten_zip("tao-journeys", gtfs_dir(hass) / "empty.zip",
                      lambda name, text: text.splitlines()[0] + "\n" if name == "routes.txt" else None)
        where = await choose(hass, await start(hass), "source")
        folder = await choose(hass, where, "source_zip")
        feeds = shown(await submit(hass, folder, file="empty"), FORM, "source_rt")
        assert shown(await submit(hass, feeds), ABORT)["reason"] == "no_routes_with_trips"
    walk(world, scenario)


def test_a_screen_refuses_what_it_does_not_offer(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        lines = shown(await to_lines(hass, "tao"), FORM, "route")
        # the line field takes typed text, which is what makes a long list
        # searchable; text that names no line brings the list back
        again = shown(await submit(hass, lines, route="the blue one"), FORM, "route")
        assert again["errors"] == {"base": "route_not_listed"}
        # text shaped like an option, naming a line the list does not hold
        # (or one held without its timetable, sent without the flag that
        # imports it): the flow read its stops from nothing and ended on
        # no_stops_read, an error in the log
        again = shown(await submit(hass, again, route="3##NOWHERE##NOWHERE : Nowhere"),
                      FORM, "route")
        assert again["errors"] == {"base": "route_not_listed"}
        with pytest.raises(vol.Invalid):
            await submit(hass, again, route=["a", "list"])
        stops = shown(await submit(hass, again, route=offered(again, "route")[1]), FORM, "stops")
        with pytest.raises(vol.Invalid):
            await submit(hass, stops, origin="ORLEANS:StopArea:0: Nowhere (0)")
        with pytest.raises(vol.Invalid):
            await submit(hass, stops, origin=offered(stops, "origin")[1], towards="x")
        # refused, the screen is still there to answer
        shown(await submit(hass, stops, origin=offered(stops, "origin")[1]), FORM, "towards")
    walk(world, scenario)


def test_a_name_in_use_is_refused_for_the_journey_and_for_its_return(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        shown(await bus_journey(hass, "tao", name="to work", add_return=True), MENU, "finished")
        outward, back = hass.journeys()
        again = shown(await bus_journey(hass, "tao", name="to work"), FORM, "sensor")
        assert again["errors"] == {"base": "name_taken"}
        assert default(again, "name") == "to work"
        # the return of the same pair is already there: it is not made again
        shown(await bus_journey(hass, "tao", name="to work again", add_return=True),
              MENU, "finished")
        assert [e.data["name"] for e in hass.journeys()] == [
            "to work", back.data["name"], "to work again"]
        # a return whose name another journey holds, for another ride
        hass.config_entries.entries.remove(back)
        held = Entry(domain=DOMAIN, title=back.data["name"],
                     data={"file": "tao", "name": back.data["name"], "route": "elsewhere"},
                     options={}, unique_id=back.unique_id, version=10, source="user")
        hass.config_entries.entries.append(held)
        again = shown(await bus_journey(hass, "tao", name="to work once more", add_return=True),
                      FORM, "sensor")
        assert again["errors"] == {"base": "return_name_taken"}
    walk(world, scenario)


def test_a_journey_the_second_flow_refuses_is_not_announced(world):
    # the import step refuses a unique_id already taken, gtfs-<name>: here
    # by an entry holding it under another name, which the screens' own
    # name check does not see
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        held = Entry(domain=DOMAIN, title="renamed", data={"file": "tao", "name": "renamed"},
                     options={}, unique_id="gtfs-home", version=10, source="user")
        hass.config_entries.entries.append(held)
        again = shown(await bus_journey(hass, "tao", name="home"), FORM, "sensor")
        assert again["errors"] == {"base": "not_created"}
        assert hass.journeys() == [held]
        refused = [r for r in hass.config_entries.flow.trail if r["type"] == ABORT]
        assert [r["reason"] for r in refused] == ["already_configured"]
    walk(world, scenario)


def test_a_journey_name_is_free_whatever_the_sources_are_called(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "gtfs-home")
        shown(await bus_journey(hass, "gtfs-home", name="home"), MENU, "finished")
    walk(world, scenario)


URL = "https://feeds.example/tao.zip"


async def source_url_screen(hass):
    menu = await start(hass)
    where = shown(await choose(hass, menu, "source"), MENU, "source")
    return shown(await choose(hass, where, "source_url"), FORM, "source_url")


def test_a_source_downloaded_from_a_url_keeps_its_address_and_its_realtime_feeds(world):
    async def scenario(hass):
        world.host.serve(URL, zip_bytes("tao-journeys"))
        # a source already known under a name of its own
        await install_source(hass, "sncf-journeys", "sncf")
        form = await source_url_screen(hass)
        # a name that would leave the folder, an address off the web
        again = await submit(hass, form, url="ftp://feeds.example/tao.zip", file="../tao")
        assert again["errors"] == {"file": "invalid_source_name", "url": "invalid_source_url"}
        assert default(again, "file") == "../tao"
        again = await submit(hass, again, url=URL, file="sncf")
        assert again["errors"] == {"file": "source_exists"}
        again = await submit(hass, again, url="https://feeds.example/moved.zip", file="tao")
        assert again["errors"] == {"base": "no_data_file"}
        feeds = shown(await submit(hass, again, url=URL, file="tao"), FORM, "source_rt")
        assert (gtfs_dir(hass) / "tao.zip").read_bytes() == zip_bytes("tao-journeys")
        meta = json.loads((gtfs_dir(hass) / "tao.zip.meta.json").read_text(encoding="utf-8"))
        assert meta["url"] == URL
        # a realtime address is held to the static one's rule, the typed
        # value kept for the rider to correct
        feeds = shown(await submit(hass, feeds, trip_update_url="htps://rt.example/trips"),
                      FORM, "source_rt")
        assert feeds["errors"] == {"trip_update_url": "invalid_source_url"}
        assert default(feeds, "trip_update_url") == "htps://rt.example/trips"
        key = shown(await submit(hass, feeds, trip_update_url=" https://rt.example/trips ",
                                 needs_api_key=True), FORM, "source_rt_key")
        assert (default(key, "api_key"), default(key, "api_key_name"),
                default(key, "api_key_location")) == ("", "api_key", "query_string")
        # "not_applicable" is not offered on a key screen; HTTP Basic came
        # after the trees that offer the two others alone
        assert offered(key, "api_key_location") in (["header", "query_string"],
                                                     ["header", "query_string", "basic"])
        shown(await submit(hass, key, api_key="rt-secret", api_key_location="header", accept=True),
              FORM, "route")
        source = hass.datasource("tao")
        assert dict(source.data) == {"kind": "datasource", "file": "tao", "url": URL,
                                     "api_key_location": "not_applicable"}
        # typed as given, stray spaces trimmed, nothing empty
        assert dict(source.options) == {
            "trip_update_url": "https://rt.example/trips", "api_key": "rt-secret",
            "api_key_name": "api_key", "api_key_location": "header", "accept": True}
    walk(world, scenario)


def test_a_source_behind_a_key_asks_for_it_and_sends_it(world):
    async def scenario(hass):
        world.host.serve(URL, zip_bytes("tao-journeys"), key=("api_key", "static-secret"))

        def downloads():
            return [r.url for r in world.host.requests if "Range" not in r.headers]

        form = await source_url_screen(hass)
        key = shown(await submit(hass, form, url=URL, file="tao", needs_api_key=True),
                    FORM, "source_key")
        # a wrong key and a wrong address fail alike: both are put right
        # on the address screen, what was typed shown again
        back = shown(await submit(hass, key, api_key="wrong"), FORM, "source_url")
        assert back["errors"] == {"base": "no_data_file"}
        assert (default(back, "url"), default(back, "file"), default(back, "needs_api_key")) \
            == (URL, "tao", True)
        assert downloads()[-1] == f"{URL}?api_key=wrong"
        key = shown(await submit(hass, back, url=URL, file="tao", needs_api_key=True),
                    FORM, "source_key")
        # the key typed before comes back in a password field, hidden on
        # screen until asked for, and sent back as it is
        assert default(key, "api_key") == "wrong"
        assert fields(key)["api_key"].config["type"] == "password"
        back = shown(await submit(hass, key, api_key="wrong"), FORM, "source_url")
        assert downloads()[-1] == f"{URL}?api_key=wrong"
        key = await submit(hass, back, url=URL, file="tao", needs_api_key=True)
        feeds = shown(await submit(hass, key, api_key="static-secret"), FORM, "source_rt")
        assert downloads()[-1] == f"{URL}?api_key=static-secret"
        shown(await submit(hass, feeds), FORM, "route")
        source = hass.datasource("tao")
        # the address is kept without its key, the key beside it
        assert dict(source.data) == {
            "kind": "datasource", "file": "tao", "url": URL,
            "api_key": "static-secret", "api_key_name": "api_key",
            "api_key_location": "query_string"}
    walk(world, scenario)


def test_a_zip_of_networks_asks_which_one_the_source_follows(world):
    async def scenario(hass):
        with zipfile.ZipFile(gtfs_dir(hass) / "networks.zip", "w") as envelope:
            envelope.writestr("bus.zip", zip_bytes("tao-journeys"))
            envelope.writestr("rail.zip", zip_bytes("sncf-journeys"))
        where = await choose(hass, await start(hass), "source")
        folder = shown(await choose(hass, where, "source_zip"), FORM, "source_zip")
        which = shown(await submit(hass, folder, file="networks"), FORM, "inner_zip")
        assert offered(which, "inner_zip") == ["bus.zip", "rail.zip"]
        assert which["description_placeholders"]["zips"] == "2"
        feeds = shown(await submit(hass, which, inner_zip="rail.zip"), FORM, "source_rt")
        assert (gtfs_dir(hass) / "networks.zip").read_bytes() == zip_bytes("sncf-journeys")
        lines = shown(await submit(hass, feeds), FORM, "route")
        assert all(r.startswith("2##") for r in offered(lines, "route"))
        assert hass.datasource("networks").data["inner_zip"] == "rail.zip"
    walk(world, scenario)


def test_a_zip_an_older_version_left_aside_does_not_stop_a_new_source(world):
    # the legacy extract renamed the zip to <source>_temp.zip while it
    # rewrote it, and the flow read that name as an unpacking under way: it
    # ended on "unpacking" and watched it. Nothing produces the name any
    # more; left over, it held the source's creation up for ever
    async def scenario(hass):
        world.host.serve(URL, zip_bytes("tao-journeys"))
        (gtfs_dir(hass) / "tao_temp.zip").write_bytes(b"")
        form = await source_url_screen(hass)
        shown(await submit(hass, form, url=URL, file="tao"), FORM, "source_rt")
        assert {request.url for request in world.host.requests} == {URL}
        assert not [t for t in hass.background_tasks if "watch" in t.get_name()]
    walk(world, scenario)


def at_a_stop(hass, file, *entity_ids):
    """Trackers standing on the first stop of a source."""
    lat, lon = rows(hass, file, "select stop_lat, stop_lon from stops order by stop_id limit 1")[0]
    for entity_id in entity_ids:
        hass.states.async_set(entity_id, "home", {"latitude": lat, "longitude": lon})


def test_the_flow_waits_for_an_unpacking_and_goes_on_with_what_was_typed(world, monkeypatch):
    real_sleep = asyncio.sleep

    async def a_moment(delay):
        await real_sleep(0.01)

    # the wait polls the files every five seconds: what is walked here is
    # the order of the screens, not the pace of the poll
    monkeypatch.setattr(reload, "asyncio", types.SimpleNamespace(
        sleep=a_moment, wait=asyncio.wait))

    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        at_a_stop(hass, "tao", "person.me")
        # a journal beside the database: something is still writing it
        journal = gtfs_dir(hass) / "tao.sqlite-journal"
        journal.write_bytes(b"")
        form = shown(await choose(hass, await start(hass), "local_stops"), FORM, "local_stops")
        flows = hass.config_entries.flow
        waiting = shown(await flows.async_configure(form["flow_id"], {
            "file": "tao", "device_tracker_id": "person.me", "name": "around me"}),
            PROGRESS, "extracting")
        assert waiting["description_placeholders"]["file"] == "tao"
        journal.unlink()
        created = shown(await flows.async_finish_progress(form["flow_id"]), CREATE)
        assert dict(created["result"].data) == {
            "file": "tao", "device_tracker_id": "person.me", "name": "around me"}
    walk(world, scenario)


def test_a_second_flow_for_the_same_tracker_is_told_why_it_stops(world, monkeypatch):
    real_sleep = asyncio.sleep

    async def a_moment(delay):
        await real_sleep(0.01)

    monkeypatch.setattr(reload, "asyncio", types.SimpleNamespace(
        sleep=a_moment, wait=asyncio.wait))

    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        at_a_stop(hass, "tao", "person.me")
        journal = gtfs_dir(hass) / "tao.sqlite-journal"
        journal.write_bytes(b"")
        answer = {"file": "tao", "device_tracker_id": "person.me", "name": "around me"}
        first = await choose(hass, await start(hass), "local_stops")
        shown(await hass.config_entries.flow.async_configure(first["flow_id"], answer),
              PROGRESS, "extracting")
        second = await choose(hass, await start(hass), "local_stops")
        refused = shown(await submit(hass, second, **{**answer, "name": "me again"}), ABORT)
        assert refused["reason"] == "already_in_progress"
        journal.unlink()
        assert refused["reason"] in STRINGS["config"]["abort"]
    walk(world, scenario, words=False)


def test_local_stops_take_a_person_or_a_zone_once_per_source(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        at_a_stop(hass, "tao", "person.me", "zone.work")
        form = shown(await choose(hass, await start(hass), "local_stops"), FORM, "local_stops")
        assert offered(form, "file") == ["tao"]
        with pytest.raises(vol.Invalid):
            await submit(hass, form, file="tao", device_tracker_id="light.kitchen", name="x")
        created = shown(await submit(hass, form, file="tao", device_tracker_id="person.me",
                                     name="around me"), CREATE)
        assert dict(created["result"].data) == {
            "file": "tao", "device_tracker_id": "person.me", "name": "around me"}
        assert created["result"].unique_id == "gtfs-local-tao-person.me"
        form = await choose(hass, await start(hass), "local_stops")
        again = shown(await submit(hass, form, file="tao", device_tracker_id="zone.work",
                                   name="around me"), FORM, "local_stops")
        assert again["errors"] == {"base": "name_taken"}
        assert default(again, "name") == "around me"
        again = shown(await submit(hass, again, file="tao", device_tracker_id="person.me",
                                   name="me again"), FORM, "local_stops")
        assert again["errors"] == {"base": "local_stops_exists"}
        shown(await submit(hass, again, file="tao", device_tracker_id="zone.work",
                           name="around work"), CREATE)
        assert len(hass.journeys()) == 2
    walk(world, scenario)


def test_a_source_s_settings_are_reached_from_the_main_menu_too(world):
    # the realtime feeds and the static refresh of a source, which only its
    # CONFIGURE button led to: the main menu names the source, then shows
    # the same screens and saves on that source (2026-09-30)
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        await install_source(hass, "sncf-journeys", "sncf")
        source = hass.datasource("tao")
        hass.config_entries.async_update_entry(source, options={"rt_enabled": False})

        pick = shown(await choose(hass, await start(hass), "source_real_time"), FORM, "pick_source")
        assert offered(pick, "file") == ["sncf", "tao"]
        feeds = shown(await submit(hass, pick, file="tao"), FORM, "real_time")
        assert default(feeds, "alerts_url") == ""
        refused = shown(await submit(hass, feeds, alerts_url="https://"), FORM, "real_time")
        assert refused["errors"] == {"alerts_url": "invalid_source_url"}
        assert "alerts_url" not in source.options
        done = shown(await submit(hass, refused, alerts_url="https://rt.example/alerts"), ABORT)
        assert done["reason"] == "source_saved"
        assert dict(source.options) == {"alerts_url": "https://rt.example/alerts",
                                        "rt_enabled": False}
        # the other source is left as it was
        assert "alerts_url" not in hass.datasource("sncf").options
        # and the source's own button reads what the menu saved
        again = await choose(hass, await options_of(hass, source), "real_time",
                             hass.config_entries.options)
        assert default(again, "alerts_url") == "https://rt.example/alerts"

        pick = shown(await choose(hass, await start(hass), "source_static_refresh"), FORM, "pick_source")
        refresh = shown(await submit(hass, pick, file="tao"), FORM, "static_refresh")
        again = shown(await submit(hass, refresh, url="ftp://feeds.example/tao.zip"),
                      FORM, "static_refresh")
        assert again["errors"] == {"url": "invalid_source_url"}
        key = shown(await submit(hass, again, url=URL, needs_api_key=True,
                                 static_refresh_mode="notify", static_check_interval=6),
                    FORM, "static_refresh_key")
        done = shown(await submit(hass, key, api_key="static-secret", api_key_name="token",
                                  api_key_location="header"), ABORT)
        assert done["reason"] == "source_saved"
        assert dict(source.data) == {
            "kind": "datasource", "file": "tao", "url": URL,
            "api_key": "static-secret", "api_key_name": "token", "api_key_location": "header"}
        assert dict(source.options) == {"alerts_url": "https://rt.example/alerts",
                                        "rt_enabled": False, "static_refresh_mode": "notify",
                                        "static_check_interval": 6}
    walk(world, scenario)


def test_a_source_file_with_no_entry_yet_has_no_settings_to_offer(world):
    # the start of Home Assistant creates a source's entry: a file without
    # one has nothing the screens could save on. The sources are listed by
    # their entries, so the menu offers nothing but adding one
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        await hass.config_entries.async_remove(hass.datasource("tao").entry_id)
        menu = shown(await start(hass), MENU, "user_empty")
        assert menu["menu_options"] == ["source"]
    walk(world, scenario)


def test_removing_a_source_takes_its_files_and_its_entry_and_leaves_the_journeys(world):
    async def scenario(hass):
        await install_source(hass, "tao-journeys", "tao")
        await install_source(hass, "sncf-journeys", "sncf")
        journey = (await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "import"},
            data={"file": "tao", "url": "na", "extract_from": "zip", "name": "to work",
                  "route": "ORLEANS:Line:40", "route_type": "3"}))["result"]
        form = shown(await choose(hass, await start(hass), "remove"), FORM, "remove")
        assert offered(form, "file") == ["sncf", "tao"]
        assert shown(await submit(hass, form, file="tao"), ABORT)["reason"] == "files_deleted"
        assert sorted(p.name for p in gtfs_dir(hass).iterdir()) == [
            "sncf.sqlite", "sncf.zip"]
        assert hass.datasource("tao") is None and hass.datasource("sncf") is not None
        assert hass.journeys() == [journey]
    walk(world, scenario)


def test_the_zip_screen_lists_the_zips_there_are(world):
    async def scenario(hass):
        where = await choose(hass, await start(hass), "source")
        ended = shown(await choose(hass, where, "source_zip"), ABORT)
        assert ended["reason"] == "no_zip_in_folder"
        assert ended["description_placeholders"]["folder"] == hass.config.path("gtfs2")
        drop_zip(hass, "tao-journeys", "tao")
        drop_zip(hass, "sncf-journeys", "sncf")
        where = await choose(hass, await start(hass), "source")
        folder = shown(await choose(hass, where, "source_zip"), FORM, "source_zip")
        assert offered(folder, "file") == ["sncf", "tao"]
        # taken away while the screen was open
        (gtfs_dir(hass) / "sncf.zip").unlink()
        again = shown(await submit(hass, folder, file="sncf"), FORM, "source_zip")
        assert again["errors"] == {"base": "no_zip_file"}
        assert offered(again, "file") == ["tao"]
    walk(world, scenario)


def test_a_source_with_an_empty_database_and_no_zip_sends_the_rider_back(world):
    async def scenario(hass):
        (gtfs_dir(hass) / "ghost.sqlite").write_bytes(b"")
        await source_entries.async_ensure_datasource_entry(hass, "ghost", api={})
        sources = shown(await choose(hass, await start(hass), "start_end"), FORM, "start_end")
        again = shown(await submit(hass, sources, file="ghost"), FORM, "start_end")
        assert again["errors"] == {"base": "no_zip_file"}
    walk(world, scenario)


# --- the options of an entry ------------------------------------------------------------

async def imported(hass, data):
    """An entry made by the flow's own import step."""
    return (await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data=data))["result"]


def test_a_journey_s_options_hold_its_own_knobs_only(world):
    async def scenario(hass):
        options = hass.config_entries.options
        entry = await imported(hass, {
            "file": "tao", "url": "na", "extract_from": "zip", "name": "to work",
            "route": "ORLEANS:Line:40", "route_type": "3", "origin": "a: A", "destination": "b: B"})
        # the realtime a journey kept before its source held it: the
        # source's now, which an edit of the journey's own knobs lets go
        hass.config_entries.async_update_entry(entry, options={
            "trip_update_url": "https://rt.example/trips", "real_time": True})
        form = shown(await options_of(hass, entry), FORM, "init")
        assert (default(form, "refresh_interval"), default(form, "offset")) == (
            const.DEFAULT_REFRESH_INTERVAL, const.DEFAULT_OFFSET)
        with pytest.raises(vol.Invalid):
            await submit(hass, form, options, refresh_interval="soon")
        shown(await submit(hass, form, options, refresh_interval=5, offset=2), CREATE)
        assert dict(entry.options) == {"refresh_interval": 5, "offset": 2}
    walk(world, scenario)


def test_a_train_journey_s_options_get_on_or_off_at_more_stations(world):
    # a week of works on a sensor made long before: K8+ trains from Les
    # Aubrais, not Orleans. Its options screen takes the station as well
    async def scenario(hass):
        options = hass.config_entries.options
        await install_source(hass, "sncf-journeys", "sncf")
        base = {"file": "sncf", "route_type": "2", "route": "train", "direction": "0",
                "origin": "Orléans", "destination": "Paris Austerlitz"}
        entry = await imported(hass, {**base, "name": "sncf K8+ Orléans → Paris Austerlitz",
                                      "line": "K8+", "lines": ["K8+"],
                                      "origin_stations": ["Orléans"],
                                      "destination_stations": ["Paris Austerlitz"]})
        form = shown(await options_of(hass, entry), FORM, "init")
        assert "Les Aubrais" in offered(form, "also_at")
        assert default(form, "also_at") == []
        # a station ticked is got on or off at: kept at both ends
        shown(await submit(hass, form, options, refresh_interval=5, also_at=["Les Aubrais"]), CREATE)
        # the entry keeps the stations, the options its own knobs only
        assert entry.data["origin_stations"] == ["Orléans", "Les Aubrais"]
        assert entry.data["destination_stations"] == ["Paris Austerlitz", "Les Aubrais"]
        assert "also_at" not in entry.options and entry.options["refresh_interval"] == 5
        # shown again ticked
        assert default(shown(await options_of(hass, entry), FORM, "init"), "also_at") == ["Les Aubrais"]
        # an entry made when the screen asked the two apart: the stations of
        # either end ticked
        apart = await imported(hass, {**base, "name": "sncf K8+ Orléans → Paris Austerlitz apart",
                                      "line": "K8+", "lines": ["K8+"],
                                      "origin_stations": ["Orléans", "Les Aubrais"],
                                      "destination_stations": ["Paris Austerlitz"]})
        assert default(shown(await options_of(hass, apart), FORM, "init"), "also_at") == ["Les Aubrais"]
        # a train journey taken over from the stock integration rides every
        # line from one station a end: it takes a second one the same way
        stock = await imported(hass, {**base, "name": "sncf Orléans → Paris Austerlitz"})
        form = shown(await options_of(hass, stock), FORM, "init")
        shown(await submit(hass, form, options, also_at=["Les Aubrais"]), CREATE)
        assert stock.data["origin_stations"] == ["Orléans", "Les Aubrais"]
        assert "line" not in stock.data
    walk(world, scenario)


def test_local_stops_options_refuse_a_radius_holding_more_stops_than_allowed(world):
    async def scenario(hass):
        options = hass.config_entries.options
        await install_source(hass, "tao-journeys", "tao")
        at_a_stop(hass, "tao", "person.me")
        entry = await imported(hass, {"file": "tao", "device_tracker_id": "person.me",
                                      "name": "around me", "url": "na", "extract_from": "zip"})
        form = shown(await options_of(hass, entry), FORM, "init")
        assert {key: default(form, key) for key in fields(form)} == {
            "local_stop_refresh_interval": const.DEFAULT_LOCAL_STOP_REFRESH_INTERVAL,
            "radius": const.DEFAULT_LOCAL_STOP_RADIUS,
            "timerange": const.DEFAULT_LOCAL_STOP_TIMERANGE,
            "offset": const.DEFAULT_OFFSET, "max_local_stops": const.DEFAULT_MAX_LOCAL_STOPS}
        with pytest.raises(vol.Invalid):
            await submit(hass, form, options, radius=40)
        refused = shown(await submit(hass, form, options, radius=5000, max_local_stops=1), ABORT)
        assert refused["reason"] == "stop_limit_reached"
        assert dict(entry.options) == {}
        form = await options_of(hass, entry)
        shown(await submit(hass, form, options, radius=50), CREATE)
        assert dict(entry.options) == {
            "local_stop_refresh_interval": const.DEFAULT_LOCAL_STOP_REFRESH_INTERVAL,
            "radius": 50, "timerange": const.DEFAULT_LOCAL_STOP_TIMERANGE,
            "offset": const.DEFAULT_OFFSET, "max_local_stops": const.DEFAULT_MAX_LOCAL_STOPS}
    walk(world, scenario)


def test_a_source_s_options_hold_its_realtime_feeds_and_its_static_refresh(world):
    async def scenario(hass):
        options = hass.config_entries.options
        await install_source(hass, "tao-journeys", "tao")
        source = hass.datasource("tao")
        # the realtime switch is not on these screens: it rides through
        hass.config_entries.async_update_entry(source, options={"rt_enabled": False})
        menu = shown(await options_of(hass, source), MENU, "source_menu")
        assert menu["menu_options"] == ["real_time", "static_refresh"]
        feeds = shown(await choose(hass, menu, "real_time", options), FORM, "real_time")
        assert {key: default(feeds, key) for key in fields(feeds)} == {
            "trip_update_url": "", "vehicle_position_url": "",
            "vehicle_max_age": const.DEFAULT_VEHICLE_MAX_AGE, "alerts_url": "",
            "needs_api_key": False}
        shown(await submit(hass, feeds, options, alerts_url="https://rt.example/alerts"), CREATE)
        # the vehicle age limit is stored only when it is not the default
        assert dict(source.options) == {"alerts_url": "https://rt.example/alerts",
                                        "rt_enabled": False}
        feeds = await choose(hass, await options_of(hass, source), "real_time", options)
        shown(await submit(hass, feeds, options, vehicle_max_age=30), CREATE)
        assert dict(source.options) == {"alerts_url": "https://rt.example/alerts",
                                        "vehicle_max_age": 30, "rt_enabled": False}

        feeds = await choose(hass, await options_of(hass, source), "real_time", options)
        assert default(feeds, "alerts_url") == "https://rt.example/alerts"
        key = shown(await submit(hass, feeds, options, trip_update_url="https://rt.example/trips",
                                 needs_api_key=True), FORM, "real_time_key")
        shown(await submit(hass, key, options, api_key="rt-secret", api_key_location="header",
                           accept=True), CREATE)
        assert dict(source.options) == {
            "trip_update_url": "https://rt.example/trips", "alerts_url": "https://rt.example/alerts",
            "vehicle_max_age": 30,
            "api_key": "rt-secret", "api_key_name": "api_key", "api_key_location": "header",
            "accept": True, "rt_enabled": False}
        realtime = dict(source.options)

        refresh = shown(await choose(hass, await options_of(hass, source), "static_refresh", options),
                        FORM, "static_refresh")
        assert {key: default(refresh, key) for key in fields(refresh)} == {
            "url": _zip_url(hass, "tao"), "needs_api_key": False, "static_refresh_mode": "off",
            "static_check_interval": const.DEFAULT_STATIC_CHECK_INTERVAL}
        assert offered(refresh, "static_refresh_mode") == const.STATIC_REFRESH_MODES
        for wrong in ({"static_check_interval": 0}, {"static_refresh_mode": "weekly"}):
            with pytest.raises(vol.Invalid):
                await submit(hass, refresh, options, url=URL, **wrong)
        again = shown(await submit(hass, refresh, options, url="ftp://feeds.example/tao.zip"),
                      FORM, "static_refresh")
        assert again["errors"] == {"url": "invalid_source_url"}
        # a feed on this machine is an address like any other
        shared = "file:///share/feeds/tao.zip"
        shown(await submit(hass, again, options, url=shared), CREATE)
        assert source.data["url"] == shared
        again = await choose(hass, await options_of(hass, source), "static_refresh", options)
        assert default(again, "url") == shared
        shown(await submit(hass, again, options, url=URL, static_refresh_mode="auto",
                           static_check_interval=12), CREATE)
        assert dict(source.options) == {**realtime, "static_refresh_mode": "auto",
                                        "static_check_interval": 12}
        assert type(source.options["static_check_interval"]) is int
        # an address given to a zip source makes it a hosted one
        assert dict(source.data) == {"kind": "datasource", "file": "tao", "url": URL,
                                     "api_key_location": "not_applicable"}

        refresh = await choose(hass, await options_of(hass, source), "static_refresh", options)
        assert (default(refresh, "url"), default(refresh, "needs_api_key")) == (URL, False)
        key = shown(await submit(hass, refresh, options, url=URL, needs_api_key=True,
                                 static_refresh_mode="notify", static_check_interval=6),
                    FORM, "static_refresh_key")
        shown(await submit(hass, key, options, api_key="static-secret", api_key_name="token",
                           api_key_location="header"), CREATE)
        assert dict(source.data) == {
            "kind": "datasource", "file": "tao", "url": URL,
            "api_key": "static-secret", "api_key_name": "token", "api_key_location": "header"}
        assert dict(source.options) == {**realtime, "static_refresh_mode": "notify",
                                        "static_check_interval": 6}
    walk(world, scenario)


