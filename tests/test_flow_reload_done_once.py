"""The screen after an import reopens the datasource once.

Home Assistant steps on from a progress done each time the flow is
configured (data_entry_flow.async_configure loops on it), and two calls
configure it as an import ends: the screen's own, and the one set on the
end of the last wait. Both ran reload_done at once: each closed the
schedule and opened its own, one under the other ('str' object has no
attribute 'engine', the stops unread, seen on the test instance).
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

ha_stub.install()

config_flow = ha_stub.load("config_flow")
reload = ha_stub.load("flow.reload")


def _flow(tmp_path, monkeypatch, opened):
    loop = asyncio.get_running_loop()

    async def job(fn, *args):
        # a read of the disk: the other call gets in meanwhile
        await asyncio.sleep(0.01)
        return fn(*args)

    def open_datasource(path, file):
        opened.append(file)
        return types.SimpleNamespace(engine=f"schedule {len(opened)}")

    monkeypatch.setattr(reload, "open_datasource", open_datasource)
    monkeypatch.setattr(reload, "check_datasource_index", lambda *args: None)
    monkeypatch.setattr(reload, "close_schedule", lambda schedule: None)
    flow = config_flow.ConfigFlow()
    flow.hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        async_add_executor_job=job, async_create_task=loop.create_task, data={})
    flow._user_inputs = {"file": "src"}

    async def direction(user_input=None):
        return {"type": "form", "step_id": "direction", "schedule": flow._pygtfs.engine}
    flow.async_step_direction = direction
    return flow


def test_two_calls_at_once_reopen_the_datasource_once(tmp_path, monkeypatch):
    async def run():
        opened = []
        flow = _flow(tmp_path, monkeypatch, opened)
        first, second = await asyncio.gather(flow.async_step_reload_done(), flow.async_step_reload_done())
        assert opened == ["src"]
        assert first == second == {"type": "form", "step_id": "direction", "schedule": "schedule 1"}
        assert first is not second
    asyncio.run(run())


def test_the_next_import_reopens_it_again(tmp_path, monkeypatch):
    async def run():
        opened = []
        flow = _flow(tmp_path, monkeypatch, opened)
        await flow.async_step_reload_done()
        flow._import_routes = ["R1"]
        monkeypatch.setattr(reload, "import_routes", lambda *args: {"R1": 3})
        flow.hass.async_create_background_task = lambda coro, name: asyncio.get_running_loop().create_task(coro)
        monkeypatch.setattr(reload, "async_notify_import", lambda *args: asyncio.sleep(0))
        await flow.async_step_importing()
        await flow._import_job
        await flow.async_step_reload_done()
        assert opened == ["src", "src"]
    asyncio.run(run())
