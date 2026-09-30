"""Read the captured cases of the case suites.

test_route_static, test_route_combined, test_stop_static and
test_stop_combined each replay cases captured on a live install, one
folder per suite (case_route, case_route_combined, case_stop,
case_stop_combined): the inputs a function was called with and what it
answered, one text file each. This finds a case's files and reads them
back.
"""
from __future__ import annotations

import datetime
import re
import zoneinfo
from pathlib import Path

EVAL_GLOBALS = {"datetime": datetime, "zoneinfo": zoneinfo}

_CASE_NUM_RE = re.compile(r"case_(\d+)")


def discover_cases(case_root: Path) -> list[tuple[str, Path]]:
    """Every case under `case_root`, as (case_id, dir_containing_its_files).

    A case's files may be named `case_1_...` or `case_1a_/case_1b_/...`
    (a sub-letter per file, e.g. from an earlier naming convention) --
    either way, everything sharing the same case *number* is grouped
    into one case, identified as `case_1`, `case_2`, etc.

    Supports both folder layouts too:
    - grouped:  case_route/case_1/case_1..._....txt
    - flat:     case_route/case_1..._....txt directly
    """
    if not case_root.is_dir():
        return []

    cases: dict[str, Path] = {}
    for path in case_root.iterdir():
        if path.is_dir() and path.name.startswith("case_"):
            match = _CASE_NUM_RE.match(path.name)
            if match:
                cases.setdefault(f"case_{match.group(1)}", path)
        elif path.is_file():
            match = _CASE_NUM_RE.match(path.name)
            if match:
                cases.setdefault(f"case_{match.group(1)}", case_root)

    def _case_number(case_id: str) -> float:
        try:
            return int(case_id.split("_")[1])
        except (IndexError, ValueError):
            return float("inf")

    return sorted(cases.items(), key=lambda item: _case_number(item[0]))


def find_case_file(case_dir: Path, case_id: str, suffix: str) -> Path:
    """The single file for `case_id` in `case_dir` whose name ends with
    `suffix`. Matched by case number, not an exact `case_1_` prefix, so
    `case_1_...`, `case_1a_...`, `case_1b_...` etc. all count as
    belonging to `case_1` -- while still telling `case_1` apart from
    `case_10`, `case_11`, ... (a plain `str.startswith` alone would not).
    """
    case_num = case_id.split("_", 1)[1]
    prefix = f"case_{case_num}"
    matches = []
    for path in case_dir.iterdir():
        name = path.name
        if not name.startswith(prefix) or not name.endswith(suffix):
            continue
        next_char = name[len(prefix):len(prefix) + 1]
        if next_char.isdigit():
            continue  # this is case_10's file, not case_1's
        matches.append(path)
    if not matches:
        raise FileNotFoundError(f"No file for {case_id!r} ending in {suffix!r} found in {case_dir}")
    if len(matches) > 1:
        raise ValueError(f"Multiple files for {case_id!r} ending in {suffix!r} found in {case_dir}: {matches}")
    return matches[0]


def parse_literal(text: str) -> object:
    """Format: a bare Python literal (list, dict), nothing else."""
    return eval(text.strip(), EVAL_GLOBALS)  # noqa: S307 - trusted, locally captured fixture


def parse_datetime_capture(text: str) -> tuple[str, datetime.datetime]:
    """Format: an optional `label: <free text>` line, then one bare ISO
    datetime. Returns (label, instant); label is "" if absent.
    """
    lines = [line for line in text.strip().splitlines() if line.strip()]
    label = ""
    if lines and lines[0].strip().lower().startswith("label:"):
        label = lines[0].split(":", 1)[1].strip()
        lines = lines[1:]
    if not lines:
        raise ValueError("Datetime capture has no datetime line after stripping the label")
    return label, datetime.datetime.fromisoformat(lines[0].strip())


def normalize_datetimes(value):
    """Recursively convert any datetime subclass (e.g. freezegun's
    FakeDatetime, produced by code running inside a `freeze_time` block)
    into a plain `datetime.datetime` with identical field values.

    Without this, a value computed under `freeze_time` and a value
    parsed from a captured expected-output file can be equal (same
    year/month/day/hour/minute/second/tzinfo) but different *types* --
    pytest's diff then shows a class-name difference as if it were a
    real one. Generic: doesn't know or care about field names, so unlike
    filtering the printed diff text, it can't accidentally hide an
    actual difference.

    Must be called *after* the `freeze_time` block has exited:
    freezegun patches `datetime.datetime` itself to be `FakeDatetime`
    while active, so a type-check made from inside the block compares
    the patched class against itself and never triggers -- confirmed
    the hard way in test_route_combined.py.
    """
    if isinstance(value, datetime.datetime) and type(value) is not datetime.datetime:
        return datetime.datetime(
            value.year, value.month, value.day, value.hour,
            value.minute, value.second, value.microsecond, value.tzinfo,
        )
    if isinstance(value, dict):
        return {k: normalize_datetimes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_datetimes(v) for v in value]
    return value
