"""A progress screen hands Home Assistant one wait at a time.

Home Assistant calls a progress step again when a progress task it has
not seen yet finishes (data_entry_flow, the done callback set on a new
progress_task), and keeps every one it was handed. The import and the
wait for another writer gave it a new 3 s wait at each call, and the
screen calls the step itself each time the size it shows changes: every
such call started one more chain of calls that never ended. At the end
of an import all of them ran what follows at once (database locked, the
schedule closed under another chain, no stops read). The same wait is
now handed for as long as it runs.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

ha_stub.install()

config_flow = ha_stub.load("config_flow")


def _flow(tmp_path):
    loop = asyncio.get_running_loop()

    async def job(fn, *args):
        return fn(*args)

    flow = config_flow.ConfigFlow()
    flow.hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        async_add_executor_job=job, async_create_task=loop.create_task)
    flow._user_inputs = {"file": "src"}
    flow._import_routes = ["R1"]
    return flow


def test_the_import_screen_hands_one_wait_while_it_runs(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        flow._import_job = asyncio.get_running_loop().create_future()
        # the screen's own calls, one per change of the figure it shows
        handed = {id((await flow.async_step_importing())["progress_task"]) for _ in range(5)}
        assert len(handed) == 1
        flow._import_job.set_result({"R1": 3})
        await asyncio.sleep(0)
        assert (await flow.async_step_importing())["type"] == "progress_done"
    asyncio.run(run())


def test_a_new_wait_once_the_last_one_ended(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        flow._extract_job = asyncio.get_running_loop().create_future()
        first = (await flow.async_step_extracting())["progress_task"]
        first.cancel()
        await asyncio.sleep(0)
        assert (await flow.async_step_extracting())["progress_task"] is not first
        flow._extract_job.cancel()
        await asyncio.sleep(0)
    asyncio.run(run())
