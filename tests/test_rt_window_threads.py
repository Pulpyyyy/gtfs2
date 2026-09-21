"""The realtime window's cache, read by every coordinator at once.

Each coordinator runs the gate in an executor thread, and on the first
cycle of the day they all do: one cleaning the cache while another added
to it raised, and each read the same envelope for itself.
"""
from __future__ import annotations

import datetime
import threading
import time
import types

import ha_stub

rt_window = ha_stub.load("rt_window")


def test_one_envelope_per_day_whoever_asks(monkeypatch):
    read = []

    def envelope(schedule, day):
        read.append(day)
        time.sleep(0.05)          # a query takes a while: the others arrive meanwhile
        return (6 * 3600, 23 * 3600)

    monkeypatch.setattr(rt_window, "_service_envelope", envelope)
    monkeypatch.setattr(rt_window, "_edition_of", lambda hass, file: "e1")
    rt_window._ENVELOPES.clear()
    day = datetime.date(2026, 9, 22)
    errors = []

    def ask():
        try:
            rt_window._window_for(None, "src", object(), day)
        except Exception as ex:  # pylint: disable=broad-except
            errors.append(ex)

    def clean():
        # what the gate does before reading: drop the days gone by
        for n in range(200):
            rt_window._ENVELOPES[("old", "e0", f"2020-01-{n % 28 + 1:02d}")] = None
            with rt_window._ENVELOPES_LOCK:
                for key in [k for k in rt_window._ENVELOPES if k[0] == "old"]:
                    del rt_window._ENVELOPES[key]

    threads = [threading.Thread(target=ask) for _ in range(8)] + [threading.Thread(target=clean)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert read == ["2026-09-22"]
