"""What refresh_source counts as a refresh that delivered.

refresh_source turns the answers of refresh_datasource into one yes or
no, and the update entity tells the user its install failed on a no. A
database swapped in, route by route or whole, is a yes; anything else
built nothing. The legacy extract answered "extracting" for an unpacking
left running, counted a yes; no refresh goes that way any more, and a
string is a no.
"""
from __future__ import annotations

import json

import ha_stub

source_refresh = ha_stub.load("source_refresh")


ANSWERS = [
    ({"R1": 12}, True),
    # a whole build names its lines, without counts
    ({"R1": None}, True),
    ("extracting", False),
    (None, False),
    (False, False),
    ("no_data_file", False),
    ("no_zip_file", False),
]


def test_refresh_source_answer(monkeypatch):
    for answer, delivered in ANSWERS:
        recorded = []
        monkeypatch.setattr(source_refresh, "refresh_datasource", lambda hass, path, data: answer)
        monkeypatch.setattr(source_refresh, "record_installed", lambda hass, file: recorded.append(file))
        assert source_refresh.refresh_source(None, "gtfs2", {"file": "src"}) is delivered, answer
        # only a database built by the swap is recorded as installed
        assert recorded == (["src"] if isinstance(answer, dict) else []), answer


def _hass(root):
    return ha_stub.config_at(root)


def _write(path, meta):
    path.write_text(json.dumps(meta))


def test_same_bytes_carry_the_validators_to_the_installed_record(tmp_path):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    (gtfs_dir / "src.sqlite").write_bytes(b"")
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


def test_a_database_gone_is_built_again_from_the_kept_zip(tmp_path):
    # lost to an error or a restore, with no record of what it was built
    # from: the record falls back on the zip's, the two read alike, and
    # the button downloaded the feed again rather than build the zip there
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    (gtfs_dir / "src.zip").write_bytes(b"zip")
    _write(gtfs_dir / "src.zip.meta.json", {"sha256": "abc", "last_modified": "NEW"})
    assert source_refresh.rebuild_pending(_hass(tmp_path), "src")
    (gtfs_dir / "src.sqlite").write_bytes(b"")
    assert not source_refresh.rebuild_pending(_hass(tmp_path), "src")
