"""What the fixes of Settings > Repairs do for gtfs2's issues.

A failed update is tried again: the fix starts the source's refresh on its
own, since a rebuild takes minutes, and ends so the issue goes; a source
removed meanwhile aborts, the issue kept. A line no sensor reads is
dropped from its source, that line alone, every other line kept: refused,
the issue kept, when a sensor reads the source whole or the line again,
when a refresh holds the source, or when it is the source's last line.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

repairs = ha_stub.load("repairs")
services = ha_stub.load("datasource_services")


class _Hass:
    def __init__(self) -> None:
        self.tasks = []
        self.config = types.SimpleNamespace(path=lambda *parts: "/config/" + "/".join(parts))

    def async_create_background_task(self, coro, name):
        coro.close()
        self.tasks.append(name)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _flow(issue_id, data, hass):
    flow = asyncio.run(repairs.async_create_fix_flow(hass, issue_id, data))
    flow.hass, flow.issue_id, flow.data = hass, issue_id, data
    return flow


def _step(flow, user_input=None):
    return asyncio.run(flow.async_step_init(user_input) if user_input is None
                       else flow.async_step_confirm(user_input))


# --- a failed update ------------------------------------------------------------

def test_retrying_asks_first_then_starts_the_refresh(monkeypatch):
    hass = _Hass()
    entry = types.SimpleNamespace(data={"file": "tao"})
    started = []

    async def refresh(hass_, entry_):
        started.append(entry_)

    monkeypatch.setattr(repairs, "datasource_entry", lambda hass_, file: entry if file == "tao" else None)
    monkeypatch.setattr(repairs, "async_rebuild_source", refresh)
    flow = _flow("refresh_failed_tao", {"file": "tao"}, hass)
    form = _step(flow)
    assert (form["type"], form["step_id"]) == ("form", "confirm")
    assert form["description_placeholders"] == {"file": "tao"}
    assert hass.tasks == []
    done = _step(flow, {})
    assert done["type"] == "create_entry"
    assert hass.tasks == ["gtfs2 refresh tao"]


def test_retrying_rebuilds_from_the_kept_zip_when_it_is_ahead(monkeypatch):
    # a nightly refresh adopted the new zip and failed to build it: the
    # retry builds that zip, as the button does, rather than downloading
    # the same feed again from a host that may be down
    source_refresh = ha_stub.load("data.source_refresh")
    for pending in (True, False):
        hass = _Hass()
        started, asked = [], []
        hass.async_create_background_task = lambda coro, name: started.append(coro)
        entry = types.SimpleNamespace(data={"file": "tao"})

        async def refresh(hass_, entry_, *, use_zip=False):
            asked.append(use_zip)
            return True

        monkeypatch.setattr(repairs, "datasource_entry", lambda hass_, file: entry)
        monkeypatch.setattr(source_refresh, "rebuild_pending", lambda hass_, file: pending)
        monkeypatch.setattr(source_refresh, "async_refresh_source", refresh)
        _step(_flow("refresh_failed_tao", {"file": "tao"}, hass), {})
        asyncio.run(started[0])
        assert asked == [pending]


def test_retrying_a_source_gone_aborts(monkeypatch):
    monkeypatch.setattr(repairs, "datasource_entry", lambda hass_, file: None)
    flow = _flow("refresh_failed_tao", {"file": "tao"}, _Hass())
    done = _step(flow, {})
    assert (done["type"], done["reason"]) == ("abort", "source_gone")


# --- a line no sensor reads -----------------------------------------------------

def test_dropping_a_line_asks_first_then_drops_it(monkeypatch):
    asked = []

    async def prune_line(hass, file, route):
        asked.append((file, route))

    monkeypatch.setattr(repairs, "async_prune_line", prune_line)
    flow = _flow("line_orphaned_tao_R1", {"file": "tao", "route": "R1", "line": "Tram A"}, _Hass())
    form = _step(flow)
    assert form["description_placeholders"] == {"file": "tao", "line": "Tram A"}
    assert asked == []
    assert _step(flow, {})["type"] == "create_entry"
    assert asked == [("tao", "R1")]


def test_a_line_that_cannot_be_dropped_says_why(monkeypatch):
    async def prune_line(hass, file, route):
        return "line_read_again"

    monkeypatch.setattr(repairs, "async_prune_line", prune_line)
    flow = _flow("line_orphaned_tao_R1", {"file": "tao", "route": "R1", "line": "R1"}, _Hass())
    done = _step(flow, {})
    assert (done["type"], done["reason"]) == ("abort", "line_read_again")


def test_an_issue_without_a_fix_of_its_own_only_confirms():
    flow = _flow("lines_missing_tao", None, _Hass())
    assert isinstance(flow, repairs.ConfirmRepairFlow)
    assert not isinstance(flow, (repairs.RetryRefreshFlow, repairs.DropLineFlow))


# --- the prune of one line -------------------------------------------------------

def _prune(monkeypatch, *, readers=(set(), False), present=None, busy=None, train=()):
    rewrites = []

    async def rewrite(hass, gtfs_dir, filename, dry_run, work, *args):
        rewrites.append((filename, dry_run, work, args))
        return {"file": filename}, busy

    monkeypatch.setattr(services, "source_readers", lambda hass, filename: readers)

    async def train_routes(hass, gtfs_dir, filename):
        return set(train)
    monkeypatch.setattr(services, "async_train_routes", train_routes)
    monkeypatch.setattr(services, "routes_in", lambda path: present)
    monkeypatch.setattr(services, "_rewrite_source", rewrite)
    return rewrites


def test_one_line_goes_every_other_stays(monkeypatch):
    rewrites = _prune(monkeypatch, readers=({"R2"}, False), present={"R1", "R2", "R3"})
    assert asyncio.run(services.async_prune_line(_Hass(), "tao", "R1")) is None
    assert rewrites == [("tao", False, services.prune_gtfs_datasource, ({"R2", "R3"},))]


def test_what_keeps_a_line_is_said(monkeypatch):
    for readers, present, busy, reason in (
            (({"R2"}, True), {"R1", "R2"}, None, "whole_feed_in_use"),
            (({"R1"}, False), {"R1", "R2"}, None, "line_read_again"),
            ((set(), False), {"R1"}, None, "last_line"),
            ((set(), False), {"R1", "R2"}, "refresh_running", "refresh_running")):
        _prune(monkeypatch, readers=readers, present=present, busy=busy)
        assert asyncio.run(services.async_prune_line(_Hass(), "tao", "R1")) == reason
    # a train sensor holding to the code of R1 reads it
    _prune(monkeypatch, present={"R1", "R2"}, train={"R1"})
    assert asyncio.run(services.async_prune_line(_Hass(), "tao", "R1")) == "line_read_again"


def test_a_line_already_gone_is_done(monkeypatch):
    for present in ({"R2"}, set(), None):
        rewrites = _prune(monkeypatch, present=present)
        assert asyncio.run(services.async_prune_line(_Hass(), "tao", "R1")) is None
        assert rewrites == []
