"""What the user reads for a line: its number, then where it goes, each
part kept only when it says something (_route_label), labels sorted the
way a line number is read (_natural), and the mode a route_type names
(line_mode, with_modes).
"""
from __future__ import annotations

from collections.abc import Mapping
import re


def _says_something(part: str | None) -> bool:
    """Whether a name part carries anything a reader can use.

    A line number is often nothing but digits, so digits count. What does not
    count is punctuation on its own: SNCF publishes 54 lines whose long name is
    the string " -", the two ends of a route it did not fill in, and showing
    that to the user is worse than showing nothing.
    """
    return any(character.isalnum() for character in str(part or ""))


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
        parts = parts[:1] + [endpoints]
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
        return label.split(" : ")[0].split(" · ")[0].strip().casefold()

    seen: dict[str, set[str | None]] = {}
    for label, mode in zip(labels, modes):
        seen.setdefault(number(label), set()).add(mode)
    return [f"{label} ({words.get(mode, mode)})"
            if mode and len(seen[number(label)]) > 1 else label
            for label, mode in zip(labels, modes)]
