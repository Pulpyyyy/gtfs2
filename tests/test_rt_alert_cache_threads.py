"""The stop caches of the alerts survive their coordinators' threads.

Every coordinator of a source runs the alerts in a thread of its own, and
the first to see a new edition of the database empties the source's
entries while the others fill them. The emptying walked the cache as it
changed ("dictionary changed size during iteration"), and a reader that
tested for a key then read it could find it gone: the alerts of that
cycle were lost.
"""
from __future__ import annotations

import itertools
import sys
import threading

import ha_stub

alerts = ha_stub.load("alerts")


def test_emptying_while_filling(monkeypatch):
    editions = itertools.count()
    monkeypatch.setattr(alerts, "_edition_of", lambda schedule: next(editions))
    data = {"file": "src", "schedule": object()}
    errors = []
    stop = threading.Event()

    def fill():
        i = 0
        while not stop.is_set():
            alerts._STOP_ALIASES[("src", str(i))] = frozenset({str(i)})
            alerts._STOP_NAMES[("src", str(i))] = "name"
            i += 1

    def empty():
        for _ in range(200):
            try:
                alerts.forget_stale_stops(data)
            except Exception as ex:  # pylint: disable=broad-except
                errors.append(repr(ex))

    for i in range(20000):
        alerts._STOP_ALIASES[("src", f"seed{i}")] = frozenset()
    # switch threads as often as the interpreter will, so the filler cuts in
    # while the cache is being walked
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    filler = threading.Thread(target=fill)
    filler.start()
    try:
        empty()
    finally:
        stop.set()
        filler.join()
        sys.setswitchinterval(interval)
        for cache in (alerts._STOP_ALIASES, alerts._STOP_NAMES):
            cache.clear()
    assert errors == []
