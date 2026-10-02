"""Lines that would read the same in a list, told apart (set_lines_apart):
by their operator, by their two ends, by the period they run in, the
lines whose days are over left out, and the list sorted the way a line
number is read.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections import Counter, defaultdict
from datetime import date
from typing import TYPE_CHECKING

from .line_ends import _Spans, look_alike_ends, route_spans
from .line_labels import _natural, _says_something, line_mode

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule


def set_lines_apart(options: list[str], agencies: list[str | None], schedule: Schedule | None,
                    gtfs_dir: str | None, filename: str | None, route_ids: list[str]) -> list[str]:
    """The line options, told apart where they read the same, in the order a
    line number is read; agencies holds each option's agency name, schedule
    is None when there is no database yet.

    Lines of two operators under one number get the agency's name; lines one
    operator publishes under one name get their two ends; what still reads
    the same is the same line published once per period of validity: the
    dead ones go, the rest say which period. The periods are asked only when
    some line is still ambiguous, so a feed that names its lines properly
    never pays for the dates of any of them. Sorted on what the user reads:
    a cast on route_id is 0 for every id that is not a number, which is most
    of them outside a small network.
    """
    options = _set_apart(options, agencies)
    options = _set_apart_by_ends(
        options, look_alike_ends(schedule, gtfs_dir, filename, _look_alikes(options)))
    if _look_alikes(options):
        spans = route_spans(gtfs_dir, filename, route_ids)
        options = _leave_out_expired(options, spans)
        options = _set_apart_by_span(options, spans)
    return sorted(options, key=lambda value: _natural(value.split("##")[2]))


def _set_apart(options: list[str], agencies: list[str | None]) -> list[str]:
    """Name the agency where two lines of the list read the same.

    options are "route_type##route_id##label[##pruned]" values, agencies the
    agency name of each, in the same order. A feed covering a region lists
    lines of several operators under one number: the metro 1 of the RATP and
    the bus 1 of Terres d'Envol at IDFM, the tram 4 of GVB and of HTM in the
    Netherlands. Those get " · <agency>" after their label, and only those:
    a label nobody else wears keeps its words. Nor is it added where the
    agencies of the look-alikes are the same too (SNCF's "INCONNU" lines),
    since it would lengthen every one of them without telling them apart.
    """
    labels = [option.split("##")[2] for option in options]
    groups: dict[str, set[str]] = {}
    for label, agency in zip(labels, agencies):
        groups.setdefault(label.casefold(), set()).add(str(agency or "").strip().casefold())
    out = []
    for option, label, agency in zip(options, labels, agencies):
        if len(groups[label.casefold()]) > 1 and _says_something(agency):
            parts = option.split("##")
            parts[2] = f"{label} · {str(agency).strip()}"
            option = "##".join(parts)
        out.append(option)
    return out


def _look_alikes(options: list[str]) -> list[str]:
    """The route_ids of the lines whose label another line of the same mode
    wears too, after _set_apart: one operator publishing one name for
    several routes (IDFM's three "TER : TER Centre - Val de Loire", to
    Chartres, to Montargis and to Châteaudun). Look-alikes of different
    modes are left out, the flow already names their mode (with_modes):
    Zou's P18 train and P18 coach."""
    labels = Counter(_shown_as(option) for option in options)
    return [option.split("##")[1] for option in options if labels[_shown_as(option)] > 1]


def _shown_as(option: str) -> tuple[str, str | None]:
    """What tells two options apart before the flow adds the mode."""
    return (option.split("##")[2].casefold(), line_mode(option.split("##")[0]))


def _leave_out_expired(options: list[str], spans: _Spans, today: str | None = None) -> list[str]:
    """Drop a line whose days are over when a live line wears its label.

    Publishers who cut their feed by period of validity list one line per
    period: the Dutch national feed had 46 lines twice, once for the day
    the old timetable ended and once for the months that follow, reading
    exactly the same. Picking the wrong one gives a sensor that will never
    have a departure.

    Only the ones a live twin stands for are dropped. A whole feed can be
    out of date - two of the eighteen sources surveyed were, one by
    seventeen months - and there the list has to keep showing the lines it
    has, expired or not, rather than going empty.
    """
    today = today or date.today().strftime("%Y%m%d")

    def over(option: str) -> bool:
        span = spans.get(option.split("##")[1])
        return span is not None and span[1] < today

    alive: defaultdict[tuple[str, str | None], bool] = defaultdict(bool)
    for option in options:
        alive[_shown_as(option)] |= not over(option)
    return [option for option in options
            if not (over(option) and alive[_shown_as(option)])]


def _read_date(stamp: str) -> str | None:
    """A GTFS date as the user reads it, or None when it is not one."""
    try:
        return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])).isoformat()
    except (TypeError, ValueError):
        return None


def _set_apart_by_span(options: list[str], spans: _Spans) -> list[str]:
    """Give the look-alikes that remain the days they run.

    What is left after the agency, the ends and the expired ones: the same
    line published once per period of validity, which is how Brisbane lists
    eighteen entries for its airport line, some of them lasting a single
    day. Their dates are the only thing that differs, so their dates are
    what the list shows.

    Only where they differ: look-alikes running the very same days are told
    apart by nothing here, and the dates would lengthen every one of them
    for no reader's benefit - the rail replacement runs Leipzig publishes
    under one name, all of them dated the same twelvemonth.
    """
    periods: defaultdict[tuple[str, str | None], set[tuple[str, str] | None]] = defaultdict(set)
    for option in options:
        periods[_shown_as(option)].add(spans.get(option.split("##")[1]))
    out = []
    for option in options:
        parts = option.split("##")
        span = spans.get(parts[1])
        if len(periods[_shown_as(option)]) > 1 and span:
            first, last = _read_date(span[0]), _read_date(span[1])
            if first and last:
                parts[2] = f"{parts[2]} · {first}" + (f" → {last}" if last != first else "")
                option = "##".join(parts)
        out.append(option)
    return out


def _set_apart_by_ends(options: list[str], ends: Mapping[str, str]) -> list[str]:
    """Give the look-alikes their two ends: " · Chartres ↔ Gare Montparnasse".

    ends is {route_id: ends} as route_ends or headsign_ends read them. A
    label that already shows its ends (a line without a long name) is left
    as it is, the words would only be said twice.
    """
    out = []
    for option in options:
        parts = option.split("##")
        found = ends.get(parts[1])
        if found and found.casefold() not in parts[2].casefold():
            parts[2] = f"{parts[2]} · {found}"
            option = "##".join(parts)
        out.append(option)
    return out
