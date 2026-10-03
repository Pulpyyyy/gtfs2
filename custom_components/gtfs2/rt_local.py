"""The update_gtfs_rt_local service: one realtime feed downloaded to a
local file (get_gtfs_rt), a SIRI stop-monitoring answer read into trip
updates on the way (convert_realtime_siri_trips_to_json).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import json
import logging
import os
from typing import Any
from urllib.parse import quote

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import requests

from .const import id_of
from .rt_feed import _feed_body, get_gtfs_feed_entities
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
            log("Issues with downloading GTFS RT SIRI data to: %s with error: %s", os.path.join(gtfs_dir, file), ex)
            return "no_rt_data_file" 
    # read at every refresh of the stops around a person: a host down says
    # so once, without the stack of requests, and an error page is not
    # written first, where it replaced the last good feed on disk and the
    # readers parsed that instead
    content = _feed_body(url, _headers, "local GTFS RT")
    if content is None:
        return "no_rt_data_file"
    try:
        with open(os.path.join(gtfs_dir, file), "wb") as out:
            out.write(content)
    except OSError as ex:
        _LOGGER.error("Issues with writing GTFS RT data to: %s: %s", os.path.join(gtfs_dir, file), ex)
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
    content = _feed_body(url, headers, "SIRI")
    if content is None:
        return {"entity": []}

    json_object = json.loads(content)
    # the delivery under a Siri root (Strasbourg) or at the top (MTA)
    feed = json_object.get('Siri') or json_object
    try:
        feed_entities = feed['ServiceDelivery']['StopMonitoringDelivery'][0]['MonitoredStopVisit']
    except (KeyError, IndexError, TypeError) as ex:
        # an answer of another shape, at every refresh it keeps it: the
        # missing key says it all, the stack is this line
        _LOGGER.error("Issues getting GTFS RT SIRI data: %s", ex)
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


def _siri_epoch(told: str | None) -> float | None:
    """A SIRI time in epoch seconds, None when the host gives none."""
    return datetime.fromisoformat(told).timestamp() if told else None


def _siri_end(call: Mapping[str, Any], expected: str, aimed: str) -> dict[str, Any]:
    """One end of a SIRI call as a stop update's arrival or departure: its
    time, expected when the host knows it, aimed otherwise, and its delay,
    expected less aimed, when it gives both; {} when it gives neither.

    The delay was always written empty, and the readers then took the gap
    to the timetable instead, which needs the visit's trip and stop to be
    the timetable's own: the host's two times say it without that."""
    expected_at = _siri_epoch(call.get(expected))
    aimed_at = _siri_epoch(call.get(aimed))
    time = expected_at if expected_at is not None else aimed_at
    if time is None:
        return {}
    end: dict[str, Any] = {"time": time}
    if expected_at is not None and aimed_at is not None:
        end["delay"] = int(expected_at - aimed_at)
    return end


def _siri_trip_start(journey: Mapping[str, Any], call: Mapping[str, Any]) -> dict[str, str]:
    """start_date (YYYYMMDD) and start_time (HH:MM:SS) of a visit's trip, as
    GTFS-RT writes them.

    The day is the operating day the host names for the journey
    (DataFrameRef), else the day of the visit's own times as the host
    wrote them; the time, when the host says when the trip left its first
    stop (OriginAimedDepartureTime), past 24:00 for a trip that left after
    its operating day. Both held the visit's epoch seconds, which no reader
    could take for a service day.
    """
    frame = str((journey.get('FramedVehicleJourneyRef') or {}).get('DataFrameRef') or "")
    try:
        day = date.fromisoformat(frame[:10])
    except ValueError:
        # one of them is there: a visit with none is left out before
        told = next(call[key] for key in ('AimedDepartureTime', 'ExpectedDepartureTime',
                                          'AimedArrivalTime', 'ExpectedArrivalTime')
                    if call.get(key))
        day = datetime.fromisoformat(told).date()
    start = {"start_date": day.strftime("%Y%m%d")}
    origin = journey.get('OriginAimedDepartureTime')
    if origin:
        left = datetime.fromisoformat(origin)
        hours = left.hour + 24 * (left.date() - day).days
        start["start_time"] = f"{hours:02d}:{left.minute:02d}:{left.second:02d}"
    return start


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
    arrival = _siri_end(call, 'ExpectedArrivalTime', 'AimedArrivalTime')
    departure = _siri_end(call, 'ExpectedDepartureTime', 'AimedDepartureTime')
    if not arrival and not departure:
        _LOGGER.debug("SIRI visit of %s at %s gives no time, left out", trip_id, stop_id)
        return None
    return {
        "id": trip_id,
        "trip_update": {
            "trip": {
                "trip_id": trip_id,
                **_siri_trip_start(journey, call),
                "route_id": journey['LineRef'],
                "direction_id": str(journey['DirectionRef'])
            },
            "stop_time_update": [{
                "stop_sequence": "n.a",
                "stop_id": stop_id,
                "arrival": arrival,
                "departure": departure,
            }]
        }
    }
