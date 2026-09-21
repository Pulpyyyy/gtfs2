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
import os

import homeassistant.util.dt as dt_util
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


def _period_bound(period, field):
    """One end of an active_period, None when the feed left it open."""
    try:
        if not period.HasField(field):
            return None
    except (AttributeError, ValueError):
        # a feed read as plain data, or bindings that do not answer for a
        # scalar: zero is what an absent bound looks like there
        pass
    value = getattr(period, field, 0) or 0
    return int(value) or None


def _alert_when(alert, now_ts, until_ts):
    """Whether an alert covers the ride ahead: "now", "later" or "over".

    Networks publish works weeks in advance, so an alert says when it
    applies and the feed keeps it in place until then. Read as if it were
    current, a closure announced for next weekend outranks the delay
    happening right now and takes its sentence. The ride ahead is from now
    to the departure the sensor announces, since an alert starting just
    before it still concerns the rider. An alert with no period at all is
    current, which is what the spec says.
    """
    periods = list(getattr(alert, "active_period", None) or [])
    if not periods:
        return "now"
    later = False
    for period in periods:
        start = _period_bound(period, "start")
        end = _period_bound(period, "end")
        if (start is None or start <= until_ts) and (end is None or end >= now_ts):
            return "now"
        if start is not None and start > until_ts:
            later = True
    return "later" if later else "over"


def _rank_alerts(items):
    """The alerts of one end of the journey, worst first and without repeats.

    SNCF publishes the same alert under two ids, word for word, and the reader
    would see the sentence twice; text, cause, effect and the stops named
    together are what one can tell apart: "Travaux" at two stations is two
    alerts. The sort is stable, so at equal effect the feed's own order
    still decides, and the cap is applied last so what is kept is the worst.
    """
    seen = set()
    unique = []
    for item in items:
        key = (item.get("text", ""), item.get("cause"), item.get("effect"),
               tuple(item.get("stops") or ()))
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


# what each source's database was when its stops were last read: the two
# caches above hold what a stop is called and which station it hangs from,
# and a rebuild can rename a stop, move it under another station or drop it
_EDITIONS: dict[str, str] = {}


def _edition_of(schedule):
    """The source's database as far as a cache cares: size and last write."""
    try:
        path = schedule.engine.url.database
        stat = os.stat(path)
    except (AttributeError, OSError, TypeError):
        return "unknown"
    return f"{int(stat.st_mtime)}:{stat.st_size}"


def forget_stale_stops(data):
    """Drop what was read from an older edition of this source's database.

    The stop caches are keyed by the source's file name, which a rebuild
    keeps: a stop renamed, moved under another station or gone would have
    been named the old way until Home Assistant restarted.
    """
    data = data or {}
    file = data.get("file")
    if not file:
        return
    edition = _edition_of(data.get("schedule"))
    if _EDITIONS.get(file) == edition:
        return
    _EDITIONS[file] = edition
    # the coordinators of a source run this in threads of their own: the
    # keys are copied in one go before any goes, and one already gone is
    # no error, so a thread filling the cache meanwhile breaks nothing
    for cache in (_STOP_ALIASES, _STOP_NAMES, _ROUTE_FACTS):
        for key in [k for k in list(cache) if k[0] == file]:
            cache.pop(key, None)
    _LOGGER.debug("Stops of %s read afresh, its database has changed", file)


def _stop_aliases(data, stop_id):
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
    data = data or {}
    schedule = data.get("schedule")
    if schedule is None:
        return frozenset({stop_id})
    key = (data.get("file"), stop_id)
    # read once: tested then read, the entry could go in between
    cached = _STOP_ALIASES.get(key)
    if cached is not None:
        return cached
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
        return frozenset(aliases)
    for row in rows:
        if row[0]:
            aliases.add(str(row[0]))
    # frozen: callers read this straight out of the cache and one of them
    # used to add the arrival's own aliases to it with |=, which grew the
    # entry of one stop with the platforms of another and handed those
    # alerts to every sensor departing from there
    aliases = frozenset(aliases)
    _STOP_ALIASES[key] = aliases
    return aliases


# the agency and route_type of a line, per datasource, as _STOP_ALIASES
_ROUTE_FACTS = {}


def _route_facts(data, route_id):
    """(agency_id, route_type) of the line, as strings, None where unknown.

    An alert may name a whole agency, or every line of one kind, and
    nothing else: what the line itself is tells whether it is concerned.
    """
    data = data or {}
    schedule = data.get("schedule")
    route_id = str(route_id or "")
    if schedule is None or not route_id:
        return None, None
    key = (data.get("file"), route_id)
    cached = _ROUTE_FACTS.get(key)
    if cached is not None:
        return cached
    try:
        with schedule.engine.connect() as conn:
            row = conn.execute(
                sql_text("select agency_id, route_type from routes where route_id = :route_id"),
                {"route_id": route_id}).fetchone()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("Could not read line %s: %s", route_id, ex)
        return None, None
    facts = (None, None)
    if row is not None:
        facts = tuple(str(v) if v not in (None, "", "None") else None for v in row)
    _ROUTE_FACTS[key] = facts
    return facts


# the name a stop is shown by, per datasource, as _STOP_ALIASES
_STOP_NAMES = {}


def _stop_names(data, stop_ids):
    """The names of the stops an alert names, their station's where they
    have one, each once.

    The sentence of an alert is its header, and a header is short: IDFM
    writes "Travaux" for the closing of a station and says which one only in
    the stop it addresses the alert to. A card that shows the sentence alone
    cannot tell the rider where the works are, nor whether the stop is one
    they get on, off or change at, or one their train only passes.
    """
    data = data or {}
    schedule = data.get("schedule")
    names = []
    if schedule is None:
        return names
    for stop_id in stop_ids:
        key = (data.get("file"), str(stop_id))
        name = _STOP_NAMES.get(key)
        if name is None:
            try:
                with schedule.engine.connect() as conn:
                    row = conn.execute(
                        sql_text("select coalesce(p.stop_name, s.stop_name) from stops s "
                                 "left join stops p on p.stop_id = s.parent_station "
                                 "where s.stop_id = :stop_id"),
                        {"stop_id": str(stop_id)}).fetchone()
            except Exception as ex:  # pylint: disable=broad-except
                # the alert is still worth its sentence without the name
                _LOGGER.debug("Could not read the name of stop %s: %s", stop_id, ex)
                continue
            if not (row and row[0]):
                # a stop this source does not carry: an alert may name the
                # whole network's stops and only some are in a filtered
                # database. Not remembered, since the next edition may
                # bring it in
                continue
            name = str(row[0]).strip()
            _STOP_NAMES[key] = name
        if name and name not in names:
            names.append(name)
    return names


def _journey_stops(data, trip_id=None):
    """Every stop of the journey, from where you get on to where you get off.

    An alert can name a station in the middle of the run: a lift out of order
    where you change, a train held two stops before yours. That concerns the
    journey as much as an alert on either end does, and reading only the two
    ends dropped all of it.

    Not cached, unlike the station of a stop: the journey belongs to the next
    departure, so the key would change with every trip and the cache would only
    grow. One indexed lookup on trip_id is cheaper than that. trip_id is
    the coordinator's, for a departure that does not name its own.
    """
    data = data or {}
    schedule = data.get("schedule")
    departure = data.get("next_departure") or {}
    trip_id = departure.get("trip_id") or trip_id
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


def _alert_language(hass):
    """The language to read an alert in: the one Home Assistant is set to."""
    config = getattr(hass, "config", None)
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
                 journey_ids=None, trip_ids=(), route_facts=(None, None), direction=None):
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

    The fields of one entity hold together, as the spec reads them: an
    agency, a kind of line (route_type) or a direction other than the
    journey's make the entity about something else, and one naming an
    agency or a kind of line and nothing narrower is about the whole
    line. route_facts is the line's (agency_id, route_type), direction the
    journey's; an unknown one judges nothing. The line is compared the way
    the trip updates are (_same_route): a feed that qualifies its ids
    named the line and was read as another one.
    """
    from .gtfs_rt_helper import _same_route  # it imports this module
    journey_ids = journey_ids or set()
    route_agency, route_type = route_facts
    direction = str(direction) if str(direction) in ("0", "1") else None
    hits = {"origin": False, "destination": False, "route": False,
            "trip": False, "journey": False, "trips": [], "stops": []}
    followed = [str(t) for t in [trip_id, *trip_ids] if t]
    for x in alert.informed_entity:
        e_stop = x.stop_id if x.HasField("stop_id") else None
        e_route = x.route_id if x.HasField("route_id") else None
        e_trip = x.trip.trip_id if x.HasField("trip") else None
        e_agency = x.agency_id if x.HasField("agency_id") else None
        e_type = str(x.route_type) if x.HasField("route_type") else None
        e_direction = str(x.direction_id) if x.HasField("direction_id") else None
        if e_route is not None and not _same_route(route_id, e_route):
            continue                      # an alert about another line
        if e_agency is not None and route_agency is not None and e_agency != route_agency:
            continue                      # another operator's
        if e_type is not None and route_type is not None and e_type != route_type:
            continue                      # another kind of line
        if e_direction is not None and direction is not None and e_direction != direction:
            continue                      # the other way
        if e_trip:
            named = [t for t in followed if _same_trip(e_trip, t)]
            if not named:
                # the fields of one entity hold together: "this trip, at
                # this stop" is about that trip alone. Read field by field,
                # an alert on another train calling at your station was
                # hung on your origin
                continue
            for t in named:
                if t not in hits["trips"]:
                    hits["trips"].append(t)
                    hits["trip"] = True
            if trip_id is None or str(trip_id) not in named:
                # a later departure of the board, not the next one: its
                # stop is kept for the card, but "T2 skips your origin"
                # says nothing of the next departure's own ends
                if e_stop is not None and e_stop not in hits["stops"] and (
                        e_stop in origin_ids or e_stop in destination_ids
                        or e_stop in journey_ids):
                    hits["stops"].append(e_stop)
                continue
        if e_stop is not None and e_stop in origin_ids:
            hits["origin"] = True
        elif e_stop is not None and e_stop in destination_ids:
            hits["destination"] = True
        elif e_stop is not None and e_stop in journey_ids:
            hits["journey"] = True
        elif e_stop is None and (e_route is not None or (
                not e_trip and (e_agency is not None or e_type is not None))):
            hits["route"] = True
            continue
        else:
            continue
        # the stops of the journey it names, in the feed's order
        if e_stop not in hits["stops"]:
            hits["stops"].append(e_stop)
    return hits


def journey_alerts(coordinator, feed_entities):
    """What the alert feed says about this sensor's journey, as the
    coordinator publishes it: the worst sentence for each end, the whole
    stack behind it, and the cause and effect of the sentence shown.

    What it reads of the coordinator is read here, once: the entry's data,
    the route, the two stops, the trips on the board and hass for the
    language; feed_entities is the alert feed as fetched.
    """
    rt_alerts = {}
    if not feed_entities:
        _LOGGER.debug("No proper RT feed entities for alerts")
        return rt_alerts
    data = getattr(coordinator, "_data", None) or {}
    forget_stale_stops(data)
    route_id = coordinator._route_id
    trip_id = getattr(coordinator, "_trip_id", None)
    origin_ids = _stop_aliases(data, coordinator._stop_id)
    destination_ids = _stop_aliases(data, coordinator._destination_id)
    # the destination the flow stored can be a station name rather than an
    # id, which never matched anything; the departure knows the real one
    arrival = (data.get("next_departure") or {}).get("destination_stop_id")
    if arrival:
        # a new set: the two come out of the cache, and merging in place
        # wrote one sensor's arrival into another's entry
        destination_ids = destination_ids | _stop_aliases(data, arrival)
    journey_ids = _journey_stops(data, trip_id)
    route_facts = _route_facts(data, route_id)
    language = _alert_language(getattr(coordinator, "hass", None))
    # the trips on the board: the next departure, then the ones listed
    # behind it, so an alert naming any of them is read
    head = str(trip_id or "")
    head = head if head and head != "no_trip_information" else None
    listed = []
    for t in getattr(coordinator, "_trip_list", None) or []:
        if t and str(t) != head and str(t) not in listed:
            listed.append(str(t))
    # the ride ahead: from now to the departure the sensor announces, the
    # span an alert has to cover to be about this journey
    now_ts = int(dt_util.utcnow().timestamp())
    leaves = (data.get("next_departure") or {}).get("departure_time")
    until_ts = now_ts
    if hasattr(leaves, "timestamp"):
        until_ts = max(now_ts, int(leaves.timestamp()))
    origin_alerts = []
    destination_alerts = []
    for entity in feed_entities:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        when = _alert_when(alert, now_ts, until_ts)
        if when == "over":
            # it applied to a day gone by; the feed drops it later
            continue
        hits = _alert_scope(alert, origin_ids, destination_ids,
                            route_id, head, journey_ids, listed,
                            route_facts, getattr(coordinator, "_direction", None))
        if not any(hits.values()):
            continue
        # an alert with no readable header still carries its cause and its
        # effect, and it does not take a sentence to say that something is
        # going on
        item = {"text": _alert_text(alert.header_text, language)}
        item.update(_alert_kind(alert))
        if when == "later":
            # announced for a later day: kept, since a rider wants to know,
            # but never ahead of what is happening on this ride
            item["later_only"] = True
        stops = _stop_names(data, hits["stops"])
        if stops:
            item["stops"] = stops
        if hits["trips"]:
            # which departures of the board it names, head first
            item["trips"] = list(hits["trips"])
            if head not in hits["trips"] and not any(
                    hits[k] for k in ("origin", "destination", "route", "journey")):
                # about a later departure only: kept, ranked after what
                # concerns the next one, so it never takes its sentence
                item["later_only"] = True
        _LOGGER.debug("RT Alert for route: %s, scope: %s, alert: %s", route_id, hits, alert.header_text)
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
