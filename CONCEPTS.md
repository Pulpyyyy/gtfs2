# Concepts

This file explains, in plain words, the concepts the fork is built on. It
uses the same terms as the code and as the Glossary of
[ARCHITECTURE.md](ARCHITECTURE.md), which gives their exact definitions.
Why the fork exists is told in [WHY_FORK.md](WHY_FORK.md).

---

## Source

A source is a GTFS feed as the integration knows it.

Examples: TAO, SNCF, Palm Bus.

A source is identified by its file name. Its url may change; the name, and
so the source, stays.

Every source has a url, where its feed is fetched from: a host's
(`https://…`), or a file on this machine (`file://…`), which is how a zip
dropped in the gtfs2 folder is read. Both are checked and refreshed the
same way.

---

## Datasource

A datasource is what a source becomes:

- on disk: its zip and its database
- in Home Assistant: its datasource entry

The datasource entry owns what belongs to the source rather than to one
sensor:

- realtime feeds
- API keys
- refresh mode
- update state

```text
Source → Datasource
            ├── Journey
            └── Local stops
```

Journeys and local stops entries read their datasource.

---

## Line (route)

A GTFS route. The code says route, the screens say line.

---

## Journey

A journey is a sensor's trip on one line, from an origin to a destination,
in one direction.

Examples:

```text
Stop A → Stop B          a bus or a tram
Station A → Station B    a train
```

Each journey entry produces one sensor.

---

## Local stops

An entry that follows a person or a zone, and lists the departures of the
stops around them.

---

## Edition

A feed changes over time. Each version of it is an edition, named by its
version label: the host's `Last-Modified`, else its `ETag`, else the start
of the zip's sha256 hash, else the download date.

```text
Edition 1 → Edition 2 → Edition 3
```

A refresh replaces one edition with the next.

---

## Freshness

Freshness answers:

> Has the source changed?

The integration asks the host with one conditional request built from the
last answer's `ETag` and `Last-Modified`. When the host cannot say, the
sha256 hash of the download decides.

Freshness decides whether a rebuild is needed.

---

## Refresh mode

The refresh mode answers:

> What happens when a new edition is found?

Chosen per source:

```text
off      nothing runs by itself; the button still refreshes on demand
notify   the update entity says a new edition is available
auto     the rebuild runs at the first check that finds a change
```

Each source is checked in a night slot of its own.

---

## Feed window

The feed window answers:

> How long is the kept timetable good for?

```text
valid     the last service day is more than 7 days away
ending    it is within 7 days
expired   it is past
unknown   the zip does not say
```

---

## Realtime window

The realtime window answers:

> Is it useful to read realtime right now?

A source's realtime feeds are only read from 10 minutes before the first
passage of the day to 20 minutes after the last. A line that is not running
needs no polling.

---

## Zip

The zip is the source of truth. The database can be rebuilt from it at any
time; the database is never taken as authoritative.

```text
Zip → Database      yes
Database → Zip      never
```

---

## GTFS filter

The GTFS filter cuts the zip down to the lines some entry follows, before
anything is imported.

```text
Full feed → Filter → Reduced feed
```

Only the followed lines are imported. The zip itself is kept whole.

---

## Real database

`<file>.sqlite`, the only database sensors read.

---

## Scratch database

`<file>.import.sqlite`, the raw output of an import, deleted once the
import ends.

Purpose: build without touching anything a sensor reads.

---

## Staging database

`<file>.refresh.sqlite`, a complete database built or copied beside the
real one, about to replace it.

Purpose: check the new edition before it goes live.

---

## Swap

Puts a checked staging database in place of the real one, with one rename.

Sensors see:

- the old edition, complete

or

- the new edition, complete

Never a half-built database. Adding lines to a source does not swap: each
line is copied into the real database in one transaction, so a sensor sees
it entirely or not at all.

---

## Place

A place is where a rider can board or alight, as the setup screens offer
it: a stop, or all the stops of one station taken together.

Places are what a journey's origin and destination are picked from.

---

## Station

For trains, a station groups the records a feed files under one name: often
one per platform, sometimes the same station under several ids.

```text
Railway station
 ├── Platform 1
 └── Platform 2
```

The user picks the station. The integration reads its stops.

---

## Struck trip

A trip the realtime feed cancels, or that skips the origin. It leaves the
list of departures, and the next one takes its place.

---

## Leg

The leg file describes the ride of a journey's next departure, stop by
stop, with its times, realtime included.

```text
Origin 08:12 → Stop 08:15 → Stop 08:19 → Destination 08:24
```

It is written for map cards. The fork does not chain several vehicles into
one journey: it is not a route planner.

---

## Timetable file

Every departure of a journey over the service day under way and the two
after it, written as a file for map cards and other consumers.

---

## Direction repair

Some providers label trips of both directions with the same `direction_id`,
or scatter a few trips into the wrong one.

Direction repair checks each trip against the stop order of its direction
and of the opposite one, and relabels a trip that contradicts its own order
but follows the opposite one.

Trips whose direction is consistent are left untouched.

---

## Provider verification

A provider test checks the integration's behaviour against a real-world
feed, on every push.

Goal:

> Detect regressions before users do.

Principle:

> Every recurring bug should eventually become a regression test.
