"""What refresh_source counts as a refresh that delivered.

refresh_source turns the many answers of refresh_datasource into one yes
or no, and the update entity tells the user its install failed on a no.
A built database is a yes, and so is an unpacking left running; a feed
refused for holding only future dates built nothing and is a no, the
same answer the swap path gives it.
"""
from __future__ import annotations

import pytest

import ha_stub

source_refresh = ha_stub.load("source_refresh")


@pytest.mark.parametrize("answer, delivered", [
    ({"R1": 12}, True),
    ("extracting", True),
    (None, False),
    (False, False),
    ("no_data_file", False),
    ("no_zip_file", False),
])
def test_refresh_source_answer(monkeypatch, answer, delivered):
    recorded = []
    monkeypatch.setattr(source_refresh, "refresh_datasource", lambda hass, path, data: answer)
    monkeypatch.setattr(source_refresh, "_record_installed", lambda hass, file: recorded.append(file))
    assert source_refresh.refresh_source(None, "gtfs2", {"file": "src"}) is delivered
    # only a database built by the swap is recorded as installed
    assert recorded == (["src"] if isinstance(answer, dict) else [])
