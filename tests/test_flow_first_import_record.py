"""The first import of a new source records what its database was built from.

A source downloaded and imported from the flow had no record of its own:
its update entity read the database off the zip's sidecar and said the
version was assumed (field test of 2026-10-06, TAO). The import that
creates the database writes the record a refresh writes; one that adds
lines to a database already there leaves it as it was. The source's
update entity, which read its versions when the source was added, is
told to read them again.
"""
from __future__ import annotations

import asyncio
import json
import types

import ha_stub

ha_stub.install()

config_flow = ha_stub.load("config_flow")
reload = ha_stub.load("flow.reload")

ZIP_RECORD = {"sha256": "5a0f00c1d2e3f4a5", "size": 12, "url": "https://tao/gtfs.zip",
              "downloaded_at": "2026-10-06T10:30:00+00:00"}


def _flow(tmp_path):
    loop = asyncio.get_running_loop()

    async def job(fn, *args):
        return fn(*args)

    flow = config_flow.ConfigFlow()
    flow.hass = types.SimpleNamespace(
        data={}, config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        async_add_executor_job=job, async_create_task=loop.create_task,
        async_create_background_task=lambda coro, name=None: loop.create_task(coro))
    flow._user_inputs = {"file": "src"}
    flow._import_routes = ["R1"]
    return flow


def _imported(tmp_path, monkeypatch, database_before):
    folder = tmp_path / "gtfs2"
    folder.mkdir()
    (folder / "src.zip.meta.json").write_text(json.dumps(ZIP_RECORD))
    if database_before:
        (folder / "src.sqlite").write_bytes(b"")

    def import_routes(gtfs_dir, filename, routes, build):
        (folder / "src.sqlite").write_bytes(b"")
        return {"R1": 3}

    async def notify(*args):
        return None

    told = []
    monkeypatch.setattr(reload, "import_routes", import_routes)
    monkeypatch.setattr(reload, "async_notify_import", notify)
    monkeypatch.setattr(reload, "async_dispatcher_send", lambda hass, signal: told.append(signal))

    async def run():
        flow = _flow(tmp_path)
        while (await flow.async_step_importing())["type"] == "progress":
            await asyncio.sleep(0)
    asyncio.run(run())
    record = folder / "src.sqlite.meta.json"
    return (json.loads(record.read_text()) if record.exists() else None), told


def test_a_new_source_records_the_zip_its_database_was_built_from(tmp_path, monkeypatch):
    record, told = _imported(tmp_path, monkeypatch, database_before=False)
    assert record["sha256"] == ZIP_RECORD["sha256"] and record["built_at"]
    # its update entity read its versions before the import: told to again
    assert told == [reload.SIGNAL_SOURCE_REFRESH.format("src")]


def test_lines_added_to_a_database_leave_its_record(tmp_path, monkeypatch):
    assert _imported(tmp_path, monkeypatch, database_before=True) == (None, [])
