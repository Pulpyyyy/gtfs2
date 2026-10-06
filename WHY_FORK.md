# Why this fork exists

This fork was not created to add features for the sake of adding features.

It exists because the same classes of problems kept appearing across real
GTFS providers, in the years of issues and support requests the upstream
project ([vingerha/gtfs2](https://github.com/vingerha/gtfs2)) gathered.

The goal is therefore:

> Fix recurring problem families once,
> instead of fixing each occurrence separately.

The terms used below (source, datasource, edition, journey…) are explained
in [CONCEPTS.md](CONCEPTS.md). How the fork is built, and why, is in
[ARCHITECTURE.md](ARCHITECTURE.md); every figure quoted here comes from it,
with the commit that measured it.

---

## The core idea

The upstream project works well for many feeds.

However, GTFS providers are highly variable:

- some omit fields
- some publish inconsistent `direction_id` values
- some rebuild trip_ids every day
- some expose enormous feeds
- some update their feeds daily
- some publish broken or incomplete realtime data

A provider that works today can change tomorrow.

The fork therefore focuses on mechanisms that absorb variability rather
than handling providers one by one.

---

## Problem: a setup made for GTFS specialists

Current situation upstream: the setup screens show the feed as the feed
stores it. The user has to know how their provider names and organises
its lines and stops, and the documentation speaks the same language.

The same line and the same stop of TAO (Orléans), as each version offers
them:

| Screen | Upstream | Fork |
|---|---|---|
| Line | `0##ORLEANS:Line:A: (A - JULES VERNE - HOPITAL LA SOURCE) TAO (Orléans)` | `A : JULES VERNE - HOPITAL LA SOURCE` |
| Origin | `ORLEANS:StopArea:THOPIT2: Hôpital de La Source (0)` | `Hôpital de La Source` |
| Transport type | asked first: "all but trains" or "trains only", which sends the user down two different setups | not asked: the line picked says it |
| Operator | always asked | asked only when the source has several |

Solution: **a setup that asks what a rider knows**. The whole setup was
rewritten screen by screen:

- every screen opens with a plain question: "Which line should this sensor
  follow?", "Where is the starting point?", "Where should the transport
  data come from?"
- the first screen says what a source and a sensor are; a first-time user
  with no source yet is guided to add one
- a line reads as its number then where it goes; when the feed leaves the
  name empty, the destinations its vehicles show stand in; lines sort the
  way a line number is read, and the mode is added where two lines share
  a number
- a stop is offered by its name, once, all platforms of a station
  together; a train journey is picked station by station, by name, then
  the lines riding between them, a sensor for each; a journey can get on
  or off at more than one stop at each end
- fields carry a short explanation under them, and errors say what to do,
  in the five languages of the integration
- the screens say what will happen before it happens (a download, how
  long a large network takes) and show progress while it runs
- a zip holding several networks asks which one; only that one is
  downloaded
- disk space is shown in plain figures, and a line removed to save space
  can be brought back from the same screens

Benefits:

- a sensor set up without knowing what a `route_id` or a `stop_id` is
- fewer wrong picks, fewer support questions

---

## Problem: duplicated realtime configuration

Current situation upstream: every journey carries its own copy of the
source's realtime settings.

```text
Journey A ── RT url, API key
Journey B ── RT url, API key
Journey C ── RT url, API key
```

The same url, API key and feed are configured once per journey: ten
sensors of one source carried ten copies that drifted apart (issue #180).

Solution: the **datasource**, one config entry per source.

```text
Datasource ── realtime feeds, API keys, refresh mode, state
    ├── Journey A
    ├── Journey B
    └── Journey C
```

Journeys read what their datasource holds.

Benefits:

- one configuration
- one download per feed, read by every sensor it serves
- one state, shown on the source's own entities (update, button, switch)
- realtime read only while the timetable has a vehicle running: from 10
  minutes before the day's first passage to 20 minutes after its last, and
  longer while a late vehicle is still announced, up to two hours

---

## Problem: keeping the timetable up to date is the user's job

Current situation upstream: the only way to renew a source's timetable is
the `update_gtfs` service. The user has to:

1. write an automation that calls it, on a schedule of their own
2. type into it again what the source's setup already holds: file name,
   url, API key, the key's name and where it goes
3. accept that it downloads and rebuilds every time, changed or not
4. find out alone whether it worked, and when the kept timetable runs out

```text
Automation (written by hand) → Download → Rebuild → Nothing changed
(repeated at every run, with no word on the result)
```

Solution: **the source looks after itself**. In the source's options, one
choice (what to do with a new edition) and one frequency:

```text
off      nothing runs by itself; the rebuild button still works
notify   the update entity says a new edition is available; an event lets
         an automation install it at a time that suits
auto     the new edition is installed at the first check that finds it
```

A source is `off` until its options say otherwise, so nothing changes
behind the back of someone who just migrated.

Behind that choice:

- **Freshness.** Before downloading anything, the fork asks the host
  whether the feed changed, with one conditional request built from the
  last answer's `ETag` and `Last-Modified`. Eleven of thirteen hosts probed
  in September 2026 answer it. When a host cannot say, the download is
  compared by its sha256 hash before anything is rebuilt.
- **Timing.** Each source is checked in a night slot of its own, between
  03:00 and 05:59, so two sources never rebuild on the same minute. A check missed
  while Home Assistant was off is caught up after start.
- **Follow-up.** The update entity shows the installed edition and the
  new one, and installs it like any other update in Home Assistant. A
  diagnostic sensor says how long the kept timetable is still good for
  (valid, ending, expired). A failed refresh is raised in Settings >
  Repairs, a failed download or import with a fix that tries again, and
  clears when a refresh succeeds.

The `update_gtfs` service stays, for the automations that already use it.

Benefits:

- no automation to write, no settings typed twice
- a download only when the host has something new, a rebuild only when
  the download differs
- the user sees what is installed, what is waiting, and what went wrong

---

## Problem: a source made from a zip has no address

Current situation upstream: a source built from a zip dropped in the
`gtfs2` folder is stored with the address `"na"`, a word that stands for
"none". Every reader of the address has to know it, and such a source can
only be refreshed by hand.

Solution: **every source has an address**. A zip in the folder is
`file://<gtfs2 folder>/<name>.zip`, and the fork reads a `file://` address
the way it reads an `http://` or `https://` one: the same check, the same
download, the same refresh.

| | Upstream | Fork |
|---|---|---|
| Address of a source made from a zip in the `gtfs2` folder | `"na"` | `file:///config/gtfs2/<name>.zip` |
| Address typed in the setup | `"na"` by default, left as is for a zip | `http://`, `https://` or `file://` |
| Where the feed is read from | `extract_from`: `url` downloads the address, `zip` reads the folder | the address, whatever its scheme |
| A new edition of a zip | dropped in the folder, then `update_gtfs` with `extract_from: zip`, by hand | found by the source's check (the file's time, then its hash), installed or announced as for a hosted source |
| `update_gtfs`: address | required, `"na"` by default | optional: creates a source (left out, the zip of that name in the folder); for one that exists, its feed is fetched from it this once, a `file://` zip while the host is down |
| `update_gtfs`: `extract_from` | `url` or `zip` | removed: a call still naming it runs, and the log says the option is gone |
| A call sending `"na"` | no address with `zip`; with `url`, a download of `"na"`, which fails | read as not given, and said in the log |
| Entries holding `"na"` | stay so | rewritten at start (entry version 10.3) to the `file://` url of the zip |

Benefits:

- one way to fetch a feed, whatever its scheme, and no word standing in
  for "none"
- a zip replaced in the folder is found by the source's own check
- a feed elsewhere on the machine (a share, a script's output) can be a
  source

---

## Problem: very large GTFS feeds

Many users only follow:

- a few lines
- a few stops
- a few journeys

Yet upstream imports the whole network.

Solution: the **GTFS filter**. The fork imports only the lines some entry
follows. The zip stays the source of truth; the database is a working copy
of what the sensors read.

Measured:

| Case | Upstream | Fork |
|---|---|---|
| SNCF, one line followed: import | 262 s | 10 s |
| SNCF, one line followed: database | 167.8 MB | 5.9 MB |

Benefits:

- smaller databases
- faster imports
- lower memory usage

---

## Problem: destructive refreshes

Current situation upstream:

```text
Live database → rebuilt in place
```

While it is rebuilt, sensors read a half-built file ("unknown for five
minutes", minutes to hours on a national feed); a refresh also threw away what
shrinking the database had saved (259 MB back to 1.1 GB on a live install);
and a `DELETE` over a national `stop_times` (15 million rows) left Home
Assistant unresponsive.

Solution: **build beside, check, then swap**.

```text
scratch database   the raw import, deleted once done
    ↓
staging database   a complete candidate, built beside the real one
    ↓
checked            every line a sensor reads must still have trips
    ↓
swap               one rename over the real database
```

A refresh never rebuilds the database sensors read. Adding lines to a
source does write into it, but one transaction per line: a sensor sees a
line entirely or not at all.

Benefits:

- sensors see the old edition or the new one, never a mix
- a failed download, import or check leaves the current data in place
- the failure is raised in Settings > Repairs and clears with its cause;
  a failed download or import comes with a fix that tries again

---

## Problem: inconsistent feeds

Many providers ship:

- missing or wrong directions
- inconsistent stops
- error pages where a feed should be
- malformed realtime feeds

Solution: **repairs and checks at the points data enters**.

Examples:

- `data/direction_ids.py` relabels trips whose `direction_id` contradicts
  their own stop order (GVB trams: 30 to 40 percent of the trips of some
  lines)
- a download replaces the kept zip only once proven to be a zip: a moved
  url often keeps answering 200 with an error page
- a new edition is checked before it replaces the old one (see above)
- a failed realtime download does not overwrite the last good feed on disk

Benefits:

- more feeds work correctly
- fewer provider-specific fixes

---

## Problem: answers that stop short

Upstream's `extract_departures` service reads the sensor's query, capped at
100 rows, and sorts them into days by their UTC clock. On a line with more
than 100 departures left in the day, "tomorrow" comes back empty; a
departure at 00:30 in Paris is filed under the evening before.

Solution: the services read whole service days, in the local zone.
`extract_departures` and `extract_arrivals` return every departure, or
arrival, of today and tomorrow, then the next one after them and the last
day the feed publishes.

---

## Problem: recurring regressions

A fix that works for one provider can break another.

Solution: **provider verification**. The fork checks its behaviour against
real feeds (SNCF, TAO, GVB, Palm Bus, Adelaide, BART…) and against the
scenarios of past issues, on every push.

Goal:

> Every bug should become a test.

---

## What it costs

- Home Assistant 2026.3 or later, on Python 3.14 (upstream accepts 2023.10).
- New entities per source: an update entity, a rebuild button, a realtime
  switch and two diagnostic sensors (realtime and timetable).
- Existing entries are migrated at start-up: a datasource entry is created
  for each source, taking over its url, keys and realtime feeds, which then
  leave the journeys. Going back to upstream on the same entries is not
  kept.

---

## Principles

The fork follows a few simple rules:

1. Prefer fixing a problem family to fixing one occurrence.
2. Keep the zip as the source of truth.
3. Never destroy working data to build new data.
4. Measure before optimizing.
5. Push complexity into the software rather than into user workflows.

---

## Non-goals

The fork is not trying to:

- replace GTFS
- replace pygtfs
- solve every possible provider issue
- become a route planner

The goal is simply to make GTFS usage more robust across real-world
providers.
