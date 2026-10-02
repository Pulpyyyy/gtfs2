"""The update_gtfs_rt_local service: one realtime feed downloaded to a
local file (get_gtfs_rt), a SIRI stop-monitoring answer read into trip
updates on the way (convert_realtime_siri_trips_to_json).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import logging
import os
from typing import Any
from urllib.parse import quote

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import requests

from .const import id_of
from .key_mask import fetch
from .rt_feed import _with_user_agent, get_gtfs_feed_entities
from .rt_source import rt_headers, with_query_key

_LOGGER = logging.getLogger(__name__)


def get_gtfs_rt(hass: HomeAssistant, path: str, data: Mapping[str, Any]) -> str:
    """Get gtfs rt data."""
    _LOGGER.debug("Getting gtfs rt locally with data: %s", data)
    _headers = data.get('headers','')
    _source_format = data.get('source_format',None)                                                  
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    url = data["url"]
    file = data["file"] + ".rt"
    url = with_query_key(url, data)
    if url is None:
        # an empty url, which the service lets through: nothing to download
        _LOGGER.error("No GTFS RT url to download %s from", data["file"])
        return "no_rt_data_file"
    # NOTE: Accept asks the server for a response format and the api key
    # authenticates, so they are unrelated, yet the header is only sent when
    # the key travels in a header. A feed that needs the header and takes its
    # key in the url, or one that needs it with no key at all, never gets it.
    # Left as is for now: changing it changes behaviour for existing setups.
    _headers = rt_headers(data) or _headers
    
    if data.get('entity_for_siri',None):
        _LOGGER.debug("Getting siri RT departures with data: %s", data)
        entity = er.async_get(hass).async_get(data["entity_for_siri"])
        _LOGGER.debug("entity: %s", entity)
        _LOGGER.debug("entity cfg id: %s", entity.config_entry_id)
        config_entry = hass.config_entries.async_get_entry(entity.config_entry_id)
        cf_data = config_entry.data
        cf_options = config_entry.options
        # the id before the first ": ", whole: a stop id may hold a ":"
        # (StopPoint:OCE...), cut there the host was asked for "StopPoint"
        _stop_id = id_of(cf_data["origin"])
        _LOGGER.debug("_stop_id: %s", _stop_id)
        _LOGGER.debug("config entry data: %s, options: %s", cf_data, cf_options)
        file = data["file"] + "_rt.json"
        try:
            siri_json = convert_realtime_siri_trips_to_json(url,_headers,_stop_id)
            open(os.path.join(gtfs_dir, file), "w").write(json.dumps(siri_json))
            return "ok"
        except Exception as ex:  # pylint: disable=broad-except
            # a host that does not answer, at every refresh it fails: one
            # line says it, the stack deep in requests adds nothing; an
            # error of our own keeps its stack
            log = _LOGGER.error if isinstance(ex, requests.RequestException) else _LOGGER.exception
            log("Ìssues with downloading GTFS RT SIRI data to: %s with error: %s", os.path.join(gtfs_dir, file), ex)
            return "no_rt_data_file" 
    try:
        r = fetch("get", url, headers=_with_user_agent(_headers), allow_redirects=True, timeout=20)
        if r.status_code != 200:
            # written first, an error page replaced the last good feed on
            # disk and the readers parsed that instead
            _LOGGER.error("Ìssues with downloading GTFS RT data, error: %s, content: %s",
                          r.status_code, r.content[:200])
            return "no_rt_data_file"
        open(os.path.join(gtfs_dir, file), "wb").write(r.content)
    except Exception as ex:  # pylint: disable=broad-except
        # read at every refresh of the stops around a person: a host down
        # says so in one line each time, without the stack of requests
        log = _LOGGER.error if isinstance(ex, requests.RequestException) else _LOGGER.exception
        log("Ìssues with downloading GTFS RT data to: %s: %s", os.path.join(gtfs_dir, file), ex)
        return "no_rt_data_file"

    
    if data.get("debug_output", False):
        try:
            feed_entities = get_gtfs_feed_entities(
                url=data.get("url", None),
                headers=_headers,
                label=data.get("rt_type", "-"),
                owner=data.get("file", ""),
            )
            file_all = data["file"] + "_converted.txt"
            # check if content is json else write without format            
            try:
                open(os.path.join(gtfs_dir, file_all), "w").write(json.dumps(feed_entities, indent=4)) 
            except (TypeError, ValueError) as ex:
                _LOGGER.debug("Not writing to file as json because of error: %s", ex)
                open(os.path.join(gtfs_dir, file_all), "w").write(str(feed_entities))              
        except OSError:
            _LOGGER.info("Issues with converting GTFS RT data to JSON, output to string") 
    return "ok"   


def convert_realtime_siri_trips_to_json(url: str, headers: Mapping[str, str | None] | None,
                                        stop_id: str) -> dict[str, Any] | str:
    
    #Used for Strasbourg, but they differ on output too
    ##the Basic token is a base64 conversion of: d6452e5d-4894-4ee1-8d5b-11ce235eeef6	
    ## ZDY0NTJlNWQtNDg5NC00ZWUxLThkNWItMTFjZTIzNWVlZWY2
    ## ZDY0NTJlNWQtNDg5NC00ZWUxLThkNWItMTFjZTIzNWVlZWY2Og==    
    #_encoded = base64.b64encode(b'd6452e5d-4894-4ee1-8d5b-11ce235eeef6:').decode("utf-8") 
    #_headers = { "Authorization": f"Basic {_encoded}" }
    #url = "https://api.cts-strasbourg.eu/v1/siri/2.0/stop-monitoring?MonitoringRef=GACEN_20"

    #url = "https://bustime.mta.info/api/siri/stop-monitoring.json?key=f4f9c18e-0550-4cc7-bc36-275715015673&OperatorRef=MTA"
    
    # the url may already carry a query of its own, or none at all
    url = url + ("&" if "?" in url else "?") + f"MonitoringRef={quote(str(stop_id))}"
    response = fetch("get", url, headers=_with_user_agent(headers), timeout=20)
    if response.status_code != 200:
        _LOGGER.error("Trying to read the SIRI feed, and got response(code): %s with text: %s",
                      response.status_code, response.text[:200])
        return {"entity": []}

    json_object = json.loads(response.content)
    # the delivery under a Siri root (Strasbourg) or at the top (MTA)
    feed = json_object.get('Siri') or json_object
    try:
        feed_entities = feed['ServiceDelivery']['StopMonitoringDelivery'][0]['MonitoredStopVisit']
    except (KeyError, IndexError, TypeError) as ex:
        # an answer of another shape, at every refresh it keeps it: the
        # missing key says it all, the stack is this line
        _LOGGER.error("Ìssues getting GTFS RT SIRI data: %s", ex)
        return 'issues with getting siri data'
        
    _LOGGER.debug("Feed entities: %s", feed_entities)

    json_data: dict[str, Any] = {
        "header": {
            "gtfs_realtime_version": feed['ServiceDelivery']['StopMonitoringDelivery'][0].get('version','not_provided'),
            "timestamp": feed['ServiceDelivery']['ResponseTimestamp'],
            "incrementality": "n/a"
        },
        "entity": []
    }


    for entity in feed_entities:
        entity_dict = _siri_trip_update(entity, stop_id)
        if entity_dict is not None:
            json_data["entity"].append(entity_dict)

    _LOGGER.debug("json data: %s", json.dumps(json_data))
    return json_data


def _siri_epoch(call: Mapping[str, Any], expected: str, aimed: str) -> float | None:
    """One end of a SIRI call in epoch seconds: expected when the host knows
    it, aimed otherwise, None when it gives neither."""
    told = call.get(expected) or call.get(aimed)
    return datetime.fromisoformat(told).timestamp() if told else None


def _siri_trip_update(entity: Mapping[str, Any], stop_id: str) -> dict[str, Any] | None:
    """A monitored visit as a trip update calling at the stop; None for a
    visit with no time at all.

    A call gives its arrival, its departure, or both: the first stop of a
    line has no arrival, the last no departure. Read whole, one such visit
    raised and the stop's every other visit was lost with it.
    """
    journey = entity['MonitoredVehicleJourney']
    call = journey['MonitoredCall']
    trip_id = journey['FramedVehicleJourneyRef']['DatedVehicleJourneyRef']
    # ExpectedArrivalTime, the real one: spelt without its "a" this never
    # matched, so every arrival was read as the timetable's and no delay
    # ever showed
    arrives = _siri_epoch(call, 'ExpectedArrivalTime', 'AimedArrivalTime')
    departs = _siri_epoch(call, 'ExpectedDepartureTime', 'AimedDepartureTime')
    if arrives is None and departs is None:
        _LOGGER.debug("SIRI visit of %s at %s gives no time, left out", trip_id, stop_id)
        return None
    starts = departs if departs is not None else arrives
    return {
        "id": trip_id,
        "trip_update": {
            "trip": {
                "trip_id": trip_id,
                "start_time": starts,
                "start_date": starts,
                "route_id": journey['LineRef'],
                "direction_id": str(journey['DirectionRef'])
            },
            "stop_time_update": [{
                "stop_sequence": "n.a",
                "stop_id": stop_id,
                "arrival": {"delay": '', "time": arrives} if arrives is not None else {},
                "departure": {"delay": '', "time": departs} if departs is not None else {},
            }]
        }
    }
