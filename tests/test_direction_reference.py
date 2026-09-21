"""The pattern a direction is judged against is the one its trips ride.

The longest pattern alone stood for a direction however few trips rode
it. On the SNCF K4 a single train each way ran on past Paris, both the
same way: the two directions then read as one stop order, and the route
was left unrepaired as a direction_id without sense, while a third of its
trips were filed under the wrong way. Among the patterns nearly as long
as the longest, the one most trips follow now stands for the direction.
"""
from __future__ import annotations

import ha_stub

direction_repair = ha_stub.load("direction_repair")

TOWARD_PARIS = ("G", "A", "B", "C", "D", "E", "P", "V")


def _patterns():
    return {
        "0": {
            TOWARD_PARIS: ["d0-1", "d0-2"],
            # filed the wrong way: they ride toward G
            ("P", "E", "D", "C", "B", "A", "G"): ["d0-3", "d0-4", "d0-5"],
        },
        "1": {
            # the lone train that runs on, one stop longer than the rest
            ("Y", "X", "G", "A", "B", "C", "D", "E", "P", "V"): ["d1-long"],
            ("V", "P", "E", "D", "C", "B", "A", "G", "X"): [f"d1-{n}" for n in range(6)],
            # filed the wrong way: they ride toward P
            ("G", "A", "B", "C", "D", "E", "P"): [f"d1-w{n}" for n in range(5)],
        },
    }


def test_the_reference_is_the_pattern_most_trips_ride():
    patterns = _patterns()["1"]
    assert direction_repair._canonical(patterns) == ("V", "P", "E", "D", "C", "B", "A", "G", "X")


def test_a_lone_longer_train_no_longer_hides_the_repair():
    flips = direction_repair.plan_until_stable(_patterns())
    assert {t for t, d in flips.items() if d == "0"} == {"d1-long", *[f"d1-w{n}" for n in range(5)]}
    assert {t for t, d in flips.items() if d == "1"} == {"d0-3", "d0-4", "d0-5"}
