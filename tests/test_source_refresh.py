"""What refresh_source counts as a refresh that delivered.

refresh_source turns the many answers of refresh_datasource into one yes
or no, and the update entity tells the user its install failed on a no.
A built database is a yes, and so is an unpacking left running; a feed
refused for holding only future dates built nothing and is a no, the
same answer the swap path gives it.
"""
from __future__ import annotations

import json
import types

import ha_stub

source_refresh = ha_stub.load("source_refresh")


ANSWERS = [
    ({"R1": 12}, True),
    ("extracting", True),
    (None, False),
    (False, False),
    ("no_data_file", False),
    ("no_zip_file", False),
]


def test_refresh_source_answer(monkeypatch):
    for answer, delivered in ANSWERS:
        recorded = []
        monkeypatch.setattr(source_refresh, "refresh_datasource", lambda hass, path, data: answer)
        monkeypatch.setattr(source_refresh, "_record_installed", lambda hass, file: recorded.append(file))
        assert source_refresh.refresh_source(None, "gtfs2", {"file": "src"}) is delivered, answer
        # only a database built by the swap is recorded as installed
        assert recorded == (["src"] if isinstance(answer, dict) else []), answer


def _hass(root):
    return types.SimpleNamespace(config=types.SimpleNamespace(path=lambda *p: str(root.joinpath(*p))))


def _write(path, meta):
    path.write_text(json.dumps(meta))


def test_same_bytes_carry_the_validators_to_the_installed_record(tmp_path):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    _write(gtfs_dir / "src.zip.meta.json", {"sha256": "abc", "last_modified": "NEW"})
    _write(gtfs_dir / "src.sqlite.meta.json", {"sha256": "abc", "last_modified": "OLD"})
    source_refresh._carry_validators(_hass(tmp_path), "src")
    installed = json.loads((gtfs_dir / "src.sqlite.meta.json").read_text())
    assert installed["last_modified"] == "NEW"
    assert not source_refresh.rebuild_pending(_hass(tmp_path), "src")


def test_other_bytes_leave_the_installed_record(tmp_path):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    _write(gtfs_dir / "src.zip.meta.json", {"sha256": "new", "last_modified": "NEW"})
    _write(gtfs_dir / "src.sqlite.meta.json", {"sha256": "old", "last_modified": "OLD"})
    source_refresh._carry_validators(_hass(tmp_path), "src")
    installed = json.loads((gtfs_dir / "src.sqlite.meta.json").read_text())
    assert installed["last_modified"] == "OLD"
    assert source_refresh.rebuild_pending(_hass(tmp_path), "src")
