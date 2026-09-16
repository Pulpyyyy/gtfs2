"""What a service alert means for one sensor.

get_rt_alerts in gtfs_rt_helper reads the alert feed; everything here says
what to make of one alert for the entry at hand: what kind of message it is
(_alert_kind), whether it concerns this sensor's route, trip or stops
(_alert_scope, with the stop's siblings and the whole ride from _stop_aliases
and _journey_stops), which of the texts to show and in which language
(_alert_text, _alert_language), and how many to keep and in what order
(_rank_alerts). Nothing here fetches or parses a feed.
"""
from __future__ import annotations

import logging

from sqlalchemy.sql import text as sql_text

_LOGGER = logging.getLogger(__name__)


def _alert_kind(alert):
    """The GTFS-RT cause and effect of an alert, as their spec names.

    The feed carries far more than the sentence gtfs2 keeps: a cause out of
    twelve (STRIKE, ACCIDENT, WEATHER, CONSTRUCTION...) and an effect out of
    eleven (NO_SERVICE, DETOUR, SIGNIFICANT_DELAYS...). A card cannot draw
    "roadworks" from a free sentence, but it can from these.

    UNKNOWN_CAUSE and UNKNOWN_EFFECT are dropped: they are the proto's default
    for a field the feed never set, so publishing them would put a value on an
    attribute that has nothing to say. Absent means "the feed did not say".
    """
    out = {}
    for field in ("cause", "effect"):
        value = getattr(alert, field, None)
        if value is None:
            continue
        try:
            name = alert.DESCRIPTOR.fields_by_name[field].enum_type.values_by_number[value].name
        except Exception:  # pylint: disable=broad-except
            # an enum value this binding does not know: the spec grows, and a
            # number nobody can name is not worth failing an update over
            _LOGGER.debug("Unknown alert %s value: %s", field, value)
            continue
        if name and not name.startswith("UNKNOWN"):
            out[field] = name
    return out


# The GTFS-RT effects, most disruptive first. Several alerts reach the same
# journey at once and only one sentence fits in an attribute, so the order is
# decided here rather than left to the feed: on the SNCF feed the seasonal
# notice "Service Velos 2026" names 613 trips one by one with no cause and no
# effect, and it hid a cancellation on the same train just by coming first.
_ALERT_EFFECT_ORDER = (
    "NO_SERVICE",
    "SIGNIFICANT_DELAYS",
    "DETOUR",
    "REDUCED_SERVICE",
    "MODIFIED_SERVICE",
    "STOP_MOVED",
    "ACCESSIBILITY_ISSUE",
    "ADDITIONAL_SERVICE",
    "OTHER_EFFECT",
    "NO_EFFECT",
)


# an attribute is read at a glance on a card, not paged through
_ALERTS_KEPT = 5


def _alert_severity(item):
    """Rank of one alert. What concerns the next departure comes before what
    names a later one only; then the effect, and an effect the feed never
    stated comes last: _alert_kind drops UNKNOWN_EFFECT, so a missing key means
    the feed said nothing, not that nothing is happening."""
    later = 1 if item.get("later_only") else 0
    try:
        return (later, _ALERT_EFFECT_ORDER.index(item.get("effect")))
    except ValueError:
        return (later, len(_ALERT_EFFECT_ORDER))


def _rank_alerts(items):
    """The alerts of one end of the journey, worst first and without repeats.

    SNCF publishes the same alert under two ids, word for word, and the reader
    would see the sentence twice; text, cause and effect together are what one
    can tell apart. The sort is stable, so at equal effect the feed's own order
    still decides, and the cap is applied last so what is kept is the worst.
    """
    seen = set()
    unique = []
    for item in items:
        key = (item.get("text", ""), item.get("cause"), item.get("effect"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=_alert_severity)
    return unique[:_ALERTS_KEPT]


# the station of a stop does not change while the datasource does not, so the
# lookup is done once per stop and kept, keyed by datasource
_STOP_ALIASES = {}


def _same_trip(named, trip_id):
    """Whether an alert trip selector names the trip being watched.

    Exact first. Then the truncated form: SNCF calls a train OCESN853603F in
    its alerts and OCESN853603F1187_F:TER:... in its timetable, the same train
    under an id the alert feed cuts before the agency. The guard is that what
    follows the prefix has to be a digit, the start of that agency id, without
    which train 105 would swallow train 1052. Measured over a whole feed: 6805
    of the 7061 trips named by an alert resolve this way, none ambiguously.
    """
    if not named or not trip_id:
        return False
    if named == trip_id:
        return True
    if not trip_id.startswith(named) or len(trip_id) <= len(named):
        return False
    return trip_id[len(named)].isdigit()


def _stop_aliases(self, stop_id):
    """The ids a stop can be named by: its own, and the station above it.

    Feeds derived from NeTEx publish a station and each of its platforms as
    separate stops. The timetable is built on the platform while the alerts
    name the station, so an alert about your own station never matched the stop
    the departure came from. Reading the parent puts the two back together, and
    it is exact: nothing is guessed from the shape of an id.
    """
    stop_id = str(stop_id or "")
    if not stop_id:
        return set()
    data = getattr(self, "_data", None) or {}
    schedule = data.get("schedule")
    if schedule is None:
        return {stop_id}
    key = (data.get("file"), stop_id)
    if key in _STOP_ALIASES:
        return _STOP_ALIASES[key]
    aliases = {stop_id}
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                sql_text("select parent_station from stops where stop_id = :stop_id"),
                {"stop_id": stop_id}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        # a locked or pruned datasource is no reason to lose the alerts the
        # stop itself is named in, and a failure is not worth remembering
        _LOGGER.debug("Could not read the station of stop %s: %s", stop_id, ex)
        return aliases
    for row in rows:
        if row[0]:
            aliases.add(str(row[0]))
    _STOP_ALIASES[key] = aliases
    return aliases


def _journey_stops(self):
    """Every stop of the journey, from where you get on to where you get off.

    An alert can name a station in the middle of the run: a lift out of order
    where you change, a train held two stops before yours. That concerns the
    journey as much as an alert on either end does, and reading only the two
    ends dropped all of it.

    Not cached, unlike the station of a stop: the journey belongs to the next
    departure, so the key would change with every trip and the cache would only
    grow. One indexed lookup on trip_id is cheaper than that.
    """
    data = getattr(self, "_data", None) or {}
    schedule = data.get("schedule")
    departure = data.get("next_departure") or {}
    trip_id = departure.get("trip_id") or getattr(self, "_trip_id", None)
    first = departure.get("origin_stop_sequence")
    last = (departure.get("destination_stop_time") or {}).get("Sequence")
    if schedule is None or not trip_id or first is None or last is None:
        return set()
    stops = set()
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                sql_text("select s.stop_id, s.parent_station from stop_times st "
                         "inner join stops s on s.stop_id = st.stop_id "
                         "where st.trip_id = :trip_id "
                         "and st.stop_sequence >= :first "
                         "and st.stop_sequence <= :last"),
                {"trip_id": trip_id, "first": first, "last": last}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        # the two ends are still read without this, so a datasource that cannot
        # answer costs the middle of the journey and nothing else
        _LOGGER.debug("Could not read the stops of trip %s: %s", trip_id, ex)
        return stops
    for stop_id, parent in rows:
        stops.add(str(stop_id))
        if parent:
            stops.add(str(parent))
    return stops


def _alert_language(self):
    """The language to read an alert in: the one Home Assistant is set to."""
    config = getattr(getattr(self, "hass", None), "config", None)
    return getattr(config, "language", None) or "en"


def _alert_text(translated, language):
    """One TranslatedString, in the wanted language, as plain text.

    The order of the translations belongs to the feed, not to the reader: SNCF
    puts German first on 328 of its 440 alerts while publishing feed_lang fr,
    so taking whichever came first showed German to a French user three times
    out of four. The first translation stays the fallback, for a feed that
    labels none of them.

    This also replaces splitting the protobuf debug rendering on the literal
    text marker, which took whatever came first and dropped every colon of the
    sentence on the way out.
    """
    translations = list(translated.translation)
    if not translations:
        return ""
    wanted = (language or "").lower()
    if wanted:
        for candidate in (wanted, wanted.split("-")[0]):
            for translation in translations:
                spoken = (translation.language or "").lower()
                if spoken == candidate or spoken.split("-")[0] == candidate:
                    return translation.text.strip()
    return translations[0].text.strip()


def _alert_scope(alert, origin_ids, destination_ids, route_id, trip_id=None,
                 journey_ids=None, trip_ids=()):
    """Which end of this journey an alert names, over ALL its informed entities.

    The loop used to reassign stop_id and route_id on every turn and compare
    only once it had ended, so an alert naming ten stops was matched on the
    tenth alone: yours had to be last in the list, or the alert was dropped in
    silence. An alert for a whole network names many stops.

    origin_ids and destination_ids are sets because a stop can be named by more
    than one id, see _stop_aliases. journey_ids holds everything in between, so
    that a stop the journey merely passes through counts too. And a trip is
    read because an alert is not obliged to name a stop or a line at all: SNCF
    addresses 385 of its 440 alerts to trips alone, and looking only at stop_id
    and route_id made every one of them invisible.

    trip_ids are the trips the entity lists behind the next one: an alert
    naming the second departure of the board concerns the rider as much as
    one naming the first, and reading the head alone hid it. hits["trips"]
    says which of them, head first, so a card can hang the alert on the
    right departure.
    """
    journey_ids = journey_ids or set()
    hits = {"origin": False, "destination": False, "route": False,
            "trip": False, "journey": False, "trips": []}
    followed = [str(t) for t in [trip_id, *trip_ids] if t]
    for x in alert.informed_entity:
        e_stop = x.stop_id if x.HasField("stop_id") else None
        e_route = x.route_id if x.HasField("route_id") else None
        e_trip = x.trip.trip_id if x.HasField("trip") else None
        if e_route is not None and e_route != str(route_id):
            continue                      # an alert about another line
        if e_trip:
            for t in followed:
                if _same_trip(e_trip, t) and t not in hits["trips"]:
                    hits["trips"].append(t)
                    hits["trip"] = True
        if e_stop is not None and e_stop in origin_ids:
            hits["origin"] = True
        elif e_stop is not None and e_stop in destination_ids:
            hits["destination"] = True
        elif e_stop is not None and e_stop in journey_ids:
            hits["journey"] = True
        elif e_stop is None and e_route == str(route_id):
            hits["route"] = True
    return hits


def journey_alerts(self, feed_entities):
    """What the alert feed says about this sensor's journey, as the
    coordinator publishes it: the worst sentence for each end, the whole
    stack behind it, and the cause and effect of the sentence shown.

    self is the coordinator, read for the stops, the route, the trips on
    the board and the language; feed_entities the alert feed as fetched.
    """
    rt_alerts = {}
    if not feed_entities:
        _LOGGER.debug("No proper RT feed entities for alerts")
        return rt_alerts
    origin_ids = _stop_aliases(self, self._stop_id)
    destination_ids = _stop_aliases(self, self._destination_id)
    # the destination the flow stored can be a station name rather than an
    # id, which never matched anything; the departure knows the real one
    arrival = ((getattr(self, "_data", None) or {})
               .get("next_departure") or {}).get("destination_stop_id")
    if arrival:
        destination_ids |= _stop_aliases(self, arrival)
    journey_ids = _journey_stops(self)
    language = _alert_language(self)
    # the trips on the board: the next departure, then the ones listed
    # behind it, so an alert naming any of them is read
    head = str(getattr(self, "_trip_id", None) or "")
    head = head if head and head != "no_trip_information" else None
    listed = []
    for t in getattr(self, "_trip_list", None) or []:
        if t and str(t) != head and str(t) not in listed:
            listed.append(str(t))
    origin_alerts = []
    destination_alerts = []
    for entity in feed_entities:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        hits = _alert_scope(alert, origin_ids, destination_ids,
                            self._route_id, head, journey_ids, listed)
        if not any(hits.values()):
            continue
        # an alert with no readable header still carries its cause and its
        # effect, and it does not take a sentence to say that something is
        # going on
        item = {"text": _alert_text(alert.header_text, language)}
        item.update(_alert_kind(alert))
        if hits["trips"]:
            # which departures of the board it names, head first
            item["trips"] = list(hits["trips"])
            if head not in hits["trips"] and not any(
                    hits[k] for k in ("origin", "destination", "route", "journey")):
                # about a later departure only: kept, ranked after what
                # concerns the next one, so it never takes its sentence
                item["later_only"] = True
        _LOGGER.debug("RT Alert for route: %s, scope: %s, alert: %s", self._route_id, hits, alert.header_text)
        # an alert about the line, about the train itself, or about a stop
        # somewhere along the way speaks for the whole journey
        whole_journey = hits["route"] or hits["trip"] or hits["journey"]
        if hits["origin"] or whole_journey:
            origin_alerts.append(item)
        if hits["destination"] or whole_journey:
            destination_alerts.append(item)
    origin_alerts = _rank_alerts(origin_alerts)
    destination_alerts = _rank_alerts(destination_alerts)
    # A journey can be under several alerts at once and the strings hold one
    # sentence each, so they take the worst of them instead of whichever the
    # feed published last. The lists carry the rest, in the same order.
    if origin_alerts:
        rt_alerts["origin_stop_alerts"] = origin_alerts
        rt_alerts["origin_stop_alert"] = origin_alerts[0]["text"]
    if destination_alerts:
        rt_alerts["destination_stop_alerts"] = destination_alerts
        rt_alerts["destination_stop_alert"] = destination_alerts[0]["text"]
    # cause and effect have to describe the alert the sentence comes from.
    # Taken from two different alerts, as they were, a card that styles
    # itself on them paints a service notice as an incident. Origin first,
    # because that is the sentence a start/stop card reads.
    head = (origin_alerts or destination_alerts or [{}])[0]
    for field in ("cause", "effect"):
        if field in head:
            rt_alerts["alert_" + field] = head[field]
    return rt_alerts
