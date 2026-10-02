"""The flow reads a stop entry's id the way the sensor does.

A stop entry reads "stop_id: name (sequence)". Ids carry colons of their
own (IDFM:123) but never ": ", while names do: UK BODS writes
"A28: Kala's (East Bound)", GtfsDe "(ehem: Hohenlohestr)". The flow cut
the id at the last ": " and the sensor (id_of) at the first: picked as
origin, such a stop asked the destination list with an id no stop has,
and the flow ended on "no_destination".
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

ha_stub.install()

config_flow = ha_stub.load("config_flow")
flow_journey = ha_stub.load("flow_journey")

KALAS = "000000008CTA: A28: Kala's (East Bound) (5)"


def test_a_name_holding_a_colon_reads_whole():
    assert flow_journey._stop_name(KALAS) == "A28: Kala's (East Bound)"
    assert flow_journey._stop_name("IDFM:123: Gare (2)") == "Gare"


def test_the_destinations_are_asked_with_the_origin_s_id(monkeypatch):
    asked = []
    monkeypatch.setattr(config_flow, "get_towards", lambda schedule, route, origin: [])
    monkeypatch.setattr(config_flow, "get_destination_stop_list",
                        lambda schedule, route, _, origin, towards: asked.append(origin) or [])

    async def job(fn, *args):
        return fn(*args)

    flow = config_flow.ConfigFlow()
    flow.hass = types.SimpleNamespace(async_add_executor_job=job)
    flow._pygtfs = object()
    flow._user_inputs = {"route": "R1: 1", "origin": KALAS}
    result = asyncio.run(flow.async_step_towards())
    assert asked == ["000000008CTA"]
    assert result["reason"] == "no_destination"
