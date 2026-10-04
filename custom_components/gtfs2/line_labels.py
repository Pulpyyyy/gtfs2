"""What the user reads for a line: its number, then where it goes, each
part kept only when it says something (_route_label), labels sorted the
way a line number is read (_natural), and the mode a route_type names
(line_mode, with_modes). Lines that would read the same in a list are
told apart (set_lines_apart): by their operator, by their two ends, by
the period they run in, the lines whose days are over left out, and the
list sorted the way a line number is read.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections import Counter, defaultdict
from datetime import date
import re
from typing import TYPE_CHECKING

from .line_ends import _Spans, look_alike_ends, route_spans

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule


def _says_something(part: str | None) -> bool:
    """Whether a name part carries anything a reader can use.

    A line number is often nothing but digits, so digits count. What does not
    count is punctuation on its own: SNCF publishes 54 lines whose long name is
    the string " -", the two ends of a route it did not fill in, and showing
    that to the user is worse than showing nothing.
    """
    return any(character.isalnum() for character in str(part or ""))


# the number a feed gives a line it has no name for: SNCF files 54 lines
# (35 in its TER feed) as "INCONNU", the only such word in 50 feeds surveyed
_NO_NAME = frozenset({"inconnu"})


def _names_nothing(part: str | None) -> bool:
    """Whether a line number only says the line has none."""
    return str(part or "").strip().casefold() in _NO_NAME


def _adds_to(short: str | None, long_name: str | None) -> bool:
    """Whether the long name tells the reader more than the number does.

    IDFM writes the number again as the long name on 1837 of its 2024 lines
    ("1" / "1", "4244" / "4244"), which read "1 : 1" in the list. A long name
    that only repeats the number is treated as no long name at all, so the
    two ends of the route take its place as they do for an empty one.
    """
    return (_says_something(long_name)
            and str(long_name).strip().casefold() != str(short or "").strip().casefold())


def _route_label(short: str | None, long_name: str | None, endpoints: str | None = None,
                 route_id: str | None = None) -> str:
    """What the user reads for one line: its number, then where it goes.

    The two parts are kept only if they say something, so a line named
    "INCONNU" against a long name of " -" no longer reads "INCONNU :  -". When
    the long name is the one missing, the two ends of the route take its place,
    which is the thing the user was looking for in the first place.
    """
    parts = [str(p) for p in (short, long_name)
             if p and str(p) != "None" and _says_something(p)]
    if len(parts) == 2 and not _adds_to(short, long_name):
        # the long name repeats the number: "1 : 1" says it twice
        parts = parts[:1]
    if len(parts) < 2 and endpoints:
        # "INCONNU : Alès > Mende" says nothing the ends do not
        parts = [part for part in parts[:1] if not _names_nothing(part)] + [endpoints]
    if parts:
        return " : ".join(parts)
    return str(route_id or "")


def _natural(label: str) -> list[tuple[int, int] | tuple[int, str]]:
    """Sort key that reads 2 before 10, the way a line number is read."""
    out: list[tuple[int, int] | tuple[int, str]] = []
    for chunk in re.split(r"(\d+)", str(label)):
        out.append((1, int(chunk)) if chunk.isdigit() else (0, chunk.lower()))
    return out


# the modes a line can be named by, as translated in common.line_mode_*
LINE_MODES = ("tram", "metro", "train", "bus", "coach", "ferry", "cable_tram",
              "aerial_lift", "funicular", "trolleybus", "monorail")


# each mode: its basic route_types, and the extended ones that also mean it,
# asked in this order
_MODE_TYPES = (
    ("tram", (0,), range(900, 1000)),
    ("metro", (1,), range(400, 500)),
    ("train", (2,), range(100, 200)),
    ("bus", (3,), range(700, 800)),
    ("coach", (), range(200, 300)),
    ("ferry", (4, 1200), range(1000, 1100)),
    ("cable_tram", (5,), ()),
    ("aerial_lift", (6,), range(1300, 1400)),
    ("funicular", (7, 1400), ()),
    ("trolleybus", (11, 800), ()),
    ("monorail", (12,), ()),
)


def line_mode(route_type: str | int) -> str | None:
    """The mode of a GTFS route_type, basic or extended, or None."""
    try:
        n = int(str(route_type))
    except ValueError:
        return None
    return next((mode for mode, basic, extended in _MODE_TYPES
                 if n in basic or n in extended), None)


def line_number(label: str) -> str:
    """The number a line label starts with: "6" of "6 : Nation ↔ Charles de
    Gaulle - Étoile" and of "6 · Nation"."""
    return label.split(" : ")[0].split(" · ")[0]


def with_modes(options: list[str], words: Mapping[str, str]) -> list[str]:
    """The labels to show for route options, the mode in brackets at the end
    where lines of one number run different modes.

    IDFM lists three lines 6 once the operator is not narrowed: the metro,
    the bus that replaces it during works, and a bus of Vallée Sud Grand
    Paris. Their ends tell them apart, their mode does it at a glance:
    "6 : Nation ↔ Charles de Gaulle - Étoile (métro)". A number no other
    line wears, or worn by lines of one mode, keeps its label. words maps a
    mode to the word shown, in the user's language.
    """
    labels = [option.split("##")[2] for option in options]
    modes = [line_mode(option.split("##")[0]) for option in options]

    def number(label: str) -> str:
        return line_number(label).strip().casefold()

    seen: dict[str, set[str | None]] = {}
    for label, mode in zip(labels, modes):
        seen.setdefault(number(label), set()).add(mode)
    return [f"{label} ({words.get(mode, mode)})"
            if mode and len(seen[number(label)]) > 1 else label
            for label, mode in zip(labels, modes)]


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
    as it is, the words would only be said twice. A label that says the line
    has no name ("INCONNU") gives way to them.
    """
    out = []
    for option in options:
        parts = option.split("##")
        found = ends.get(parts[1])
        if found and found.casefold() not in parts[2].casefold():
            parts[2] = found if _names_nothing(parts[2]) else f"{parts[2]} · {found}"
            option = "##".join(parts)
        out.append(option)
    return out
