"""How long a kept feed is good for: read_feed_window and timetable_state.

The window comes out of the zip alone, no database: what feed_info.txt
says of itself, and the last day any service runs from the calendars.
Each case builds a small zip in the shapes real feeds come in: a feed_info
with dates, none at all (Zou), calendar rows with every weekday off (the
TAO writes its whole service through calendar_dates and leaves such rows
behind), removals that must narrow nothing, a zip that is not there.
"""
from __future__ import annotations

import datetime
import zipfile

import ha_stub

feed_window = ha_stub.load("feed_window")
read_feed_window = feed_window.read_feed_window
timetable_state = feed_window.timetable_state


def write_zip(path, members):
    with zipfile.ZipFile(path, "w") as zout:
        for name, body in members.items():
            zout.writestr(name, body)


CALENDAR = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WEEK,1,1,1,1,1,0,0,20260901,20261130\n"
            "SAT,0,0,0,0,0,1,0,20260905,20261219\n")
DATES = ("service_id,date,exception_type\n"
         "WEEK,20261111,2\n"      # a removal narrows nothing
         "XMAS,20261224,1\n"      # an addition counts
         "WEEK,20260825,1\n")     # an addition before the window counts too
FEED_INFO = ("feed_publisher_name,feed_publisher_url,feed_lang,feed_version,feed_start_date,feed_end_date\n"
             "Palmbus,https://example.invalid,fr,25082026,20260817,20270103\n")


def test_window_reads_feed_info_and_the_calendars(tmp_path):
    write_zip(tmp_path / "feed.zip", {"feed_info.txt": FEED_INFO, "calendar.txt": CALENDAR,
                                     "calendar_dates.txt": DATES})
    assert read_feed_window(tmp_path / "feed.zip") == {
        "feed_publisher_name": "Palmbus", "feed_version": "25082026",
        "feed_start_date": "2026-08-17", "feed_end_date": "2027-01-03",
        "first_service_day": "2026-08-25", "last_service_day": "2026-12-24",
    }


def test_window_without_feed_info_reads_the_calendars_alone(tmp_path):
    # Zou ships no feed_info.txt: the calendars still say when it ends
    write_zip(tmp_path / "feed.zip", {"calendar.txt": CALENDAR, "calendar_dates.txt": DATES})
    window = read_feed_window(tmp_path / "feed.zip")
    assert window["feed_end_date"] is None and window["feed_version"] is None
    assert (window["first_service_day"], window["last_service_day"]) == ("2026-08-25", "2026-12-24")


def test_calendar_rows_with_every_weekday_off_say_nothing(tmp_path):
    # the TAO shape: a calendar row per service, every flag 0, the dates
    # covering a year, and the real service in calendar_dates
    calendar = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "A,0,0,0,0,0,0,0,20260101,20271231\n")
    dates = "service_id,date,exception_type\nA,20260901,1\nA,20261003,1\n"
    write_zip(tmp_path / "feed.zip", {"calendar.txt": calendar, "calendar_dates.txt": dates})
    window = read_feed_window(tmp_path / "feed.zip")
    assert (window["first_service_day"], window["last_service_day"]) == ("2026-09-01", "2026-10-03")


def test_members_may_sit_in_a_folder_and_dates_may_already_be_iso(tmp_path):
    write_zip(tmp_path / "feed.zip", {"gtfs/calendar_dates.txt": "service_id,date,exception_type\nA,2026-09-01,1\n"})
    assert read_feed_window(tmp_path / "feed.zip")["last_service_day"] == "2026-09-01"


def test_no_zip_or_no_calendar_is_an_empty_answer(tmp_path):
    assert read_feed_window(tmp_path / "gone.zip") == {}
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    assert read_feed_window(tmp_path / "junk.zip") == {}
    write_zip(tmp_path / "feed.zip", {"routes.txt": "route_id\nA\n"})
    window = read_feed_window(tmp_path / "feed.zip")
    assert window["last_service_day"] is None and window["feed_end_date"] is None


def test_timetable_state_counts_the_days_left():
    window = {"last_service_day": "2026-12-24"}
    assert timetable_state(window, datetime.date(2026, 9, 15)) == ("valid", 100)
    assert timetable_state(window, datetime.date(2026, 12, 17)) == ("ending", 7)
    assert timetable_state(window, datetime.date(2026, 12, 24)) == ("ending", 0)
    assert timetable_state(window, datetime.date(2026, 12, 25)) == ("expired", -1)
    assert timetable_state({}, datetime.date(2026, 9, 15)) == ("unknown", None)
    assert timetable_state({"last_service_day": "soon"}, datetime.date(2026, 9, 15)) == ("unknown", None)


def test_two_sources_read_in_turn_keep_their_last_day(tmp_path, monkeypatch):
    # every entry of a source asks for its last day; two sources asked in
    # turn each read their zip once, and again only for a new edition
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    write_zip(a, {"calendar.txt": CALENDAR})
    write_zip(b, {"calendar_dates.txt": DATES})
    reads = []
    real = feed_window.read_feed_window
    monkeypatch.setattr(feed_window, "read_feed_window", lambda p: reads.append(p) or real(p))
    feed_window._LAST_SERVICE_DAY.clear()
    for _ in range(3):
        assert feed_window.last_service_day(str(a)) == "2026-12-19"
        assert feed_window.last_service_day(str(b)) == "2026-12-24"
    assert len(reads) == 2
    write_zip(a, {"calendar.txt": CALENDAR, "calendar_dates.txt": DATES})
    assert feed_window.last_service_day(str(a)) == "2026-12-24"
    assert len(reads) == 3
