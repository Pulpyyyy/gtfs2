# GTFS2 Architecture

This document says where each responsibility of the fork lives, **why** it
lives there, and **which path the code takes in which case**. Every figure
quoted comes from the commit that introduced the behaviour; the commit is
named so the measurement can be found again.

It describes `refactor/architecture` as of 775a9ba (2026-09-23). A commit
that adds, moves or renames a module updates this file in the same commit.

Contents: Context · Goals · How the fork is maintained · Layers · Glossary ·
Config entries · Services · Line labels and directions · A refresh cycle ·
Local stops · Realtime · Static feed · Write paths · Failure and recovery ·
Files on disk · Concurrency · API keys · Design decisions · Verification ·
Known gaps · Known defects

## Context: what the fork fixes

Upstream (vingerha/gtfs2) imports a feed with pygtfs into one SQLite file
per source, and that one file played two parts at once:

- **the file the sensors query**, every minute, from every entry;
- **the workspace an import rebuilds**, from scratch, in place.

Four problems came from that, each measured on real installs:

| Problem | Measured | Commit |
|---|---|---|
| pygtfs imports the whole network, row by row, whatever the user follows | SNCF, one route kept: import 262 s → 10 s, datasource 167.8 MB → 5.9 MB | da8f4c6 |
| A refresh rebuilt the live file: sensors read a half-built database | "unknown for five minutes"; minutes to hours on a national feed | ec0881b, gtfs_db.py |
| A refresh threw away what prune and intern had reclaimed | 259 MB back to 1.1 GB on a live install | gtfs_db.py docstring |
| DELETE on a national `stop_times` held the database | 15 M rows: Home Assistant went unresponsive | aefaae1 |

The fork's answer, in one sentence: **keep the zip as the full record,
keep in the database only what a sensor reads, and never write the file the
sensors read.** Everything below follows from that.

## Goals and non-goals

Goals:

- A sensor always reads a complete edition: the old one or the new one,
  never a mix and never a half-built file.
- The database size follows what is followed, not the size of the network.
- A failed download, import or check leaves the current data in place.
- Nothing blocking on the event loop.
- A downgrade to upstream keeps working on the same config entries.

Non-goals:

- Replacing pygtfs. It stays the loader; the fork only decides what it is
  fed and what is kept from it (`gtfs_db.py`: "pygtfs is a loader, not a
  database layer").
- Querying the whole network from a database. Line lists and headsigns of
  lines never imported are read from the zip (`route_names.py`,
  `zip_peek.py`), not imported to be read.
- Supporting Windows as a runtime. Home Assistant runs on Linux; the swap
  has a fallback for a developer's Windows box, unguarded for the few
  microseconds of the rename (`swap_in`).

Minimum Home Assistant: 2024.12 (`hacs.json`). The options flow reads
`self.config_entry` without storing it, which `OptionsFlow` provides from
2024.12 on (2024.11 has it on `OptionsFlowWithConfigEntry` only);
`entry.runtime_data` needs 2024.5.

Python: 3.12 to 3.14, what Home Assistant runs on from 2024.12 (3.12) to
today (3.14). The test workflows run both ends.

## How the fork is maintained

### Branches

```
upstream/main (vingerha)
    │
    ├── feat/*, fix/*        lots: one change each, cut on upstream main,
    │                        proposable upstream as they are
    │
    ├── integration/all-lots every lot merged together, conflicts settled once
    │
    ├── ext/rt-per-source    what cannot go upstream as a lot: datasource
    │                        entries, source-level realtime, two-database flow
    │
    └── refactor/architecture
                             ext/rt-per-source, reorganised into the layers
                             below; takes lot fixes by port, never by merging
                             ext (e.g. 32459bb → the fix of
                             _remove_entry_geojson)
```

**Upstream changes.** `upstream/main` is followed, not merged into
`refactor/architecture`. The fork no longer follows upstream's text: each
upstream change is reviewed, and taken over when it fixes something here,
written in the fork's own code. One that covers what the fork already does
another way is not taken.

Two upstream changes of September 2026, measured on the same feeds:

- d47e744 finds a train station by the start of its name
  (`stop_name LIKE 'name%'`). On the SNCF feed, 197 of the 2,819 stations
  it offers are the start of another one: Albert also matches Albertville.
  The fork matches the exact name (`stations.py`). Not taken.
- 0eb6b0a reads up to 100 rows for the departures of today and tomorrow.
  On TAO, line A leaves one stop 159 times a day, 318 over the two days.
  The fork reads both service days whole (`_route_departures_between`).
  Not taken.

A lot stays mergeable upstream on its own. When a lot is recut on a newer
upstream main, its tests become tolerant: a promise about a reader the tree
has is checked, one about a reader it lacks is recorded as not checked
(b56fde6).

### When code is moved, and why: the refactor rule

The refactor is **not** a rewrite. It has one trigger and one constraint.

**Trigger.** Code the fork owns grew inside a file or method upstream owns,
and that is where merges collide. The upstream-owned places are:

```
gtfs_helper.py                              "a file upstream owns"; five lots
                                            collided there (ea56837)
coordinator.py  _async_update_data          upstream's method (e843433)
sensor.py       _update_attrs               upstream's method (3f01c10)
gtfs_rt_helper.py                           upstream's realtime reader (52bbe62)
config_flow.py                              upstream's flow, split in screens
```

When a block the fork added there has a single responsibility, it moves to
a module the fork owns, and the upstream method calls it in one line where
the block stood. That is how `alerts.py`, `departure_attributes.py`,
`refresh_steps.py`, `route_names.py`, `notifications.py`, `geojson.py`,
`source_zip.py`, `stations.py` and the `flow_*.py` screens were born.

**Constraint.** A move changes no behaviour:

- bodies are moved unchanged, apart from `self` becoming an argument;
- one module per commit, so a regression bisects to one move;
- the module that received the code does not import back into the file it
  left, where that can be avoided (e.g. `gtfs_helper imports nothing back`,
  333c76e, ee1f15e, 49cb538);
- imports orphaned by the move are dropped in the same commit;
- the commit says what was run: `tests/`, `tests_provider/`, and for the
  sensor a harness comparing attributes, state, icon, name and attribution
  on eight frozen coordinator states before and after (3f01c10).

**What is not moved.** Upstream code that the fork does not modify stays
where upstream put it, which keeps an upstream change to it easy to review
and take over: `get_gtfs` and the legacy extract stay in `gtfs_helper.py`;
the per-import flags (`check_source_dates`, `clean_feed_info`) stay on the
journey entries
(`_JOURNEY_REFRESH_KEYS`, "kept there for upstream compatibility").

A behaviour change never rides along with a move. It gets its own commit,
before or after.

## Overview: the layers

GTFS2 is organised in five layers, plus a query module and a few shared
modules that every layer uses:

```
┌──────────────────────────┐
│ Home Assistant layer     │  entities, coordinators, setup, services
├──────────────────────────┤
│ Config flow layer        │  screens
├──────────────────────────┤
│ Domain services layer    │  what the data means for one sensor
├──────────────────────────┤
│ Data management layer    │  the database files and what is written from them
├──────────────────────────┤
│ Source & feed layer      │  where the data comes from, and when
└──────────────────────────┘

  gtfs_helper.py     reads the database for every layer (queries, get_gtfs)
  gtfs_rt_helper.py  reads the realtime feeds
  shared             const.py, key_mask.py, notifications.py
  vendored           zip_file.py, requests_testadapter.py
```

**Dependency rule (target).** A module imports from its own layer, from a
lower layer, and from the shared modules. It never imports from a higher
layer. `gtfs_helper.py` and `gtfs_rt_helper.py` are upstream's and sit
outside the layers until the gaps at the end are closed. The rule is not
enforced yet: see "Known gaps".

**Why these five.** The cut follows what changes together. The source layer
changes with hosts and publishers (validators, ranges, envelopes); the data
layer with SQLite and pygtfs; the domain layer with what a rider wants to
read; the flow with screens; the HA layer with Home Assistant's API. A
change in one should not reach two layers apart.

### 1. Home Assistant layer

```
__init__.py        setup, unload, remove, migrate, services
coordinator.py     GTFSUpdateCoordinator, GTFSLocalStopUpdateCoordinator
sensor.py          departure sensors
update.py          update entity of a source
button.py          refresh button of a source
switch.py          realtime switch of a datasource
```

- Services are registered once, in `setup()`, not per entry. Each declares
  the fields `services.yaml` lists; extra keys still pass, for automations
  written against older field lists (5adc926).
- An entry's coordinator lives on `entry.runtime_data`. `hass.data[DOMAIN]`
  holds only what the sources share: locks, probe states, check timers, the
  bootstrap flag (b960969).
- The coordinator holds no SQL: it reads the timetable through `gtfs_helper`
  and the realtime through `gtfs_rt_helper`, and hands the fork's own steps
  to `refresh_steps.py` and `exports.py`.

### 2. Config flow layer

```
config_flow.py     the flow itself, composed of the screen classes below
flow_source.py     SourceScreens: where the timetable comes from
flow_reload.py     ReloadScreens: load lines into a datasource, shrink it
flow_journey.py    JourneyScreens: direction, sensor name, mirror journey
flow_train.py      TrainScreens: arrival station and sensor
flow_options.py    OptionsScreens: realtime feeds and static refresh of a source
```

Collects input, creates datasource and journey entries, starts imports. An
import can outlive its flow window; its outcome is then told through
`notifications.py`.

### 3. Domain services layer

```
alerts.py                what a service alert means for one sensor
departure_attributes.py  the attribute groups the fork adds to a sensor
refresh_steps.py         next service date, trips struck by the realtime
route_names.py           line labels, lines a feed declares
stations.py              train entries: stations instead of stops
exports.py               which map files a refresh writes, and when
```

Functions here take values, not entities: `departure_attributes` takes the
attributes dict and what it reads, "nothing of the entity" (3f01c10).

### 4. Data management layer

```
gtfs_db.py            everything that opens a database file directly
gtfs_filter.py        cut a zip down to chosen routes before any import
direction_repair.py   repair trip direction_id after import
gtfs_shape.py         read one shape out of the zip (shapes.txt is never imported)
geojson.py            the files written under www/gtfs2 for a map card
feed_window.py        how long the kept timetable is good for
```

`gtfs_db.py` imports neither pygtfs nor Home Assistant: the scratch build is
passed in as a callable (`import_routes(..., build_scratch)`), so the module
can be tested on plain SQLite files.

### 5. Source & feed layer

```
rt_source.py        the datasource entries, owning the realtime feeds and keys
source_zip.py       the zip beside a datasource: fetched, kept, refreshed
zip_peek.py         read a remote zip's contents, take one member out of it
freshness.py        ask the host whether the feed changed, without downloading
source_refresh.py   automatic refresh of the static feeds, per source and mode
rt_window.py        when the realtime feeds are worth reading
```

## Glossary

```
source         a GTFS feed as the integration knows it, identified by its file
               name; its url can change, the name stays
datasource     what a source becomes on disk (its zip and database) and in Home
               Assistant (its datasource entry)
edition        one version of a source's feed, named by its version label
               (Last-Modified, else ETag, else sha256 prefix, else download
               date: version_label); a refresh replaces one edition with the next
line, route    a GTFS route; the code says route, the screens say line
journey        a sensor's trip on one line, from an origin to a destination, in
               one direction
local stops    an entry that follows a person and lists the departures of the
               stops around them
whole-feed     a source that some sensor reads across every line: a train
source         entry (route "train"), a local stops entry, or an entry naming
               no line (_reads_whole_feed)
real           <file>.sqlite, the only database sensors open
scratch        <file>.import.sqlite, the raw pygtfs output of an import,
               deleted when the import ends
staging        <file>.refresh.sqlite, a complete database built or copied beside
               the real one, about to be swapped in
swap           putting a staging file in place of the real one with one rename,
               under SQLite's exclusive lock (swap_in)
prune          drop the trips of routes no entry follows, and what hangs off
               them: stop_times, frequencies, trip shapes, and the calendar of a
               service no surviving trip runs on. The routes table stays whole
               so the flow keeps offering every line
intern         replace the text keys of stop_times (trip_id, stop_id) by
               integers in gtfs2_trip_key / gtfs2_stop_key, and re-expose
               stop_times as a view, so every query keeps working unchanged
optimise       prune (when a keep set is given) then intern, in that order:
               interning first would mint keys for rows about to be deleted
struck trip    a trip the realtime feed cancels or skips; it leaves the list and
               the next one takes its place
refresh mode   off | notify | auto, per source (see "Static feed")
window         the hours a source's realtime feeds are read, derived from its
               timetable (rt_window.py)
lot            a feat/ or fix/ branch cut on upstream main, one change each
```

## Config entries

Three kinds of entry, told apart in `async_setup_entry`:

```
datasource   data["kind"] == "datasource"   one per source, unique_id = file
local stops  data["device_tracker_id"] set  one per followed person
journey      anything else                  one per sensor (bus or train)
```

**Why a datasource entry.** Realtime feeds, keys and the refresh mode belong
to a source, not to a sensor. Stored per journey, as upstream does, ten
sensors of one source carried ten copies that drifted apart (issue #180,
d771cba).

**Datasource entry.** Runs no coordinator. It carries the url and key of the
static feed, the realtime feeds and their keys, the refresh mode and check
interval. Its platforms are `DATASOURCE_PLATFORMS`: the update entity and
the refresh button, the switch that silences realtime, and two diagnostic
sensors (whether realtime runs, how long the timetable is good for). It
arms the scheduled look at the source's host (`async_arm_source_check`).

Datasource entries are created at every start, in the background and
idempotently, from the disk and the journey entries already there
(`async_bootstrap_datasource_entries`). Realtime options are seeded from the
most recently modified journey entry.

**Compatibility with upstream.** The entry `VERSION` stays upstream's (10):
the fork adds a kind of entry, it does not change the schema of the others.
Every edit on a datasource entry is mirrored back onto the journey entries
of the source (`async_mirror_rt_to_entries`), so a downgrade to upstream,
which knows no datasource entry, finds current values rather than those
frozen at bootstrap. Coordinators resolve realtime through the datasource
entry first and fall back on the entry's own options, which is also what a
pre-bootstrap start reads.

**Journey entry.** Gets a `GTFSUpdateCoordinator` and one departure sensor.
A bus journey names its line, direction and stops; a train journey names
stations and stores `route = "train"`, a marker rather than a route id.

**Local stops entry.** Gets a `GTFSLocalStopUpdateCoordinator` and one sensor
per stop around the person.

Each cycle, a coordinator asks `rt_feed_config` for its realtime settings.
An edit on the datasource entry reaches every sensor of the source within a
minute, without a reload.

## Services

Registered once, in `setup()`. Four answer with a service response
(`SupportsResponse.OPTIONAL`): `extract_departures`, `extract_trip_stops`,
`prune_datasource` and `intern_datasource`.

```
update_gtfs              refresh a datasource from its own url and key, or
                         create one; runs the write paths above under the
                         source's lock
update_gtfs_rt_local     download one realtime feed to a local file (trip
                         updates, vehicles, alerts, or SIRI)
update_gtfs_local_stops  reload the local stops entries of one tracker
extract_departures       a journey entry's departures today and tomorrow from
                         from_time on, plus next (the first one after the two
                         days, None when the calendar has none) and until (the
                         last service day the feed publishes)
extract_trip_stops       the calls of each trip a sensor lists, from its origin
                         on, "name - HH:MM:SS"
prune_datasource         drop what no entry follows; dry_run says what would go
intern_datasource        replace the text keys of stop_times by integers
```

`extract_departures` reads both service days whole, not the sensor's first
rows: a busy line has more than ten departures left today, and the ten the
sensor lists all fell on today, so "tomorrow" came back empty (aafc3c6).
Without next and until, two empty lists said the same for a line that
resumes on Thursday, a line suspended and a feed that ran out (115d543).
It answers for journey entries only; a datasource or local stops entry
has no two ends and gets empty lists. `extract_trip_stops` reads the calls
off the event loop, matched by stop_id and stop_sequence (d15f022).

## Line labels and directions

What the route screen shows decides which line a sensor follows, so the
labels are built to tell lines apart, and read from the zip when the
database holds no timetable for them (`route_names.py`).

```
label          the line number, then where it goes (_route_label)
where it goes  the long name; when the feed leaves it empty, the
               destinations its trips show (headsign_ends); else the two
               ends of its longest trip (_route_endpoints)
order          the way a line number is read: 2 before 10 (_natural)
```

Lines that would still read the same are set apart, in this order:

- by the mode, where lines of one number run different modes (8d190fe);
- by their two ends, where one operator publishes several lines under one
  name: IDFM lists three "TER Centre - Val de Loire" (5449185);
- by their period of validity, where a publisher cuts its feed by period
  with one route_id per window: Brisbane lists its airport line eighteen
  times, the Dutch feed carried 46 lines twice. A line whose days are over
  is left out of a live list (f87237a).

The ends are stable across rebuilds: a tie is broken by direction and
ends, never by trip id (185183a). A short name written in capitals is kept
as a destination when it names a place of the feed, NICE or PAU, while a
mission code such as UZAR is not (f777a72). Reading `stop_times.txt` for
the ends stops above 150 MB (eb98488).

**Direction repair** (`direction_repair.py`). Every query filters on
`direction_id`, and some feeds label it wrong: GVB trams 1, 7 and 17 carry
30 to 40 % of their trips under the other direction. After each import
(`source_zip.py`, and the legacy extract), each trip is tested against the
stop order of its direction and of the opposite one, compared by station,
and relabelled when it rides the opposite one (b4c89e0, 434826c). A loop
and a line whose `direction_id` carries no sense at all (GVB tram 14) are
left as published and logged. The reference order is the pattern most
trips ride, not the longest one (18c326f).

## A refresh cycle

`GTFSUpdateCoordinator._async_update_data`, every minute (`update_interval`,
fixed). Local stops entries run on `local_stop_refresh_interval`, 15 min by
default.

```
1. source still being unpacked?        keep the previous data, flag it, stop
2. static refresh due?                 refresh_interval passed (15 min by
                                       default), or the departure shown has left
     yes:  get_next_departure            from the database, via gtfs_helper
           export_route_shape            route file, when its trip or the zip changed
           export_timetable              timetable file
           nothing left today?           next_service_date_for
     no:   keep the previous departure
3. realtime on for the source?         rt_feed_config
     rt_window_gate                    outside the window: feeds not read,
                                       vehicle file cleared
     get_rt_alerts                     alerts first, on their own
     get_next_services                 delays of the listed trips
     drop_struck_trips                 a struck trip goes, the next takes its place
   realtime off or paused:             delays and alerts emptied, never
                                       carried over from another moment
4. export_leg                          when the static ran or realtime was read
5. _read_records                       the rows the sensor describes the
                                       departure with
```

The schedule is opened by `schedule_for` and reopened only when the
database file changed: its inode, mtime and size (`_database_edition`).
Opening one is an engine plus a `create_all` over every table; every
coordinator used to do it every minute.

Step 1 relies on `check_extracting`: a `.sqlite-journal` or `_temp.zip`
beside the database. The flag is cleared once the reuse branch is reached,
so a transient journal no longer blanks the sensors for a whole refresh
interval (01587fd).

### Local stops

A local stops entry follows a person (`device_tracker_id`) and gets one
sensor per stop near them, from `GTFSLocalStopUpdateCoordinator`, every
`local_stop_refresh_interval` minutes (15 by default).

```
position       the tracker's coordinates; no tracker, or none yet: no
               stop, no error (e9d3bc0)
stops          within a box of radius metres around it (200 by default),
               turned into degrees at 111,111 m each
departures     from now minus timerange_history to now plus timerange
               (15 and 30 minutes by default), every line
realtime       trip updates only, matched by trip; the vehicle feed is not
               read, since the entry speaks for no line
```

At setup the flow refuses more stops than the entry's own limit
(`max_local_stops`, 15 by default; fb4af02). A realtime feed that fails
leaves the timetable standing, without delays (e35fe88). An entry set up
while its source is still unpacking has no stops to create yet: its
platform answers "not ready" and Home Assistant retries it. The retry
refreshes the coordinator with `async_refresh`, since Home Assistant
refuses a first refresh once the entry is loaded (1fe6d7e).

## Realtime architecture

```
rt_source.py          urls and keys of the source's feeds
        ↓
rt_window.py          rt_window_gate: is this a time the feeds are read?
        ↓
gtfs_rt_helper.py     get_next_services, get_rt_alerts
        ↓                    ↓
alerts.py             refresh_steps.py (drop_struck_trips)
        ↓
coordinator.py
        ↓
sensor.py
```

**The window.** Per source and service day, feeds are read from the first
passage of the day minus 10 minutes to the last plus 20. GTFS hours pass
24, so yesterday's window is always tested beside today's. A day without
service reads nothing. At the close, the window stretches by 10 minutes per
re-check while the last fetch still announces a future stop for a followed
line, capped two hours past the close: a late vehicle is when realtime
matters most. Why derive it: the integration owns the timetable, so nobody
has to write an automation, and episodic lines stop being polled (TAO's 22
runs 122 days a year).

**Outages.** A feed failure is logged when it is new for its url, at debug
while it lasts, and its recovery once at info (be807b9): every entry of a
source reads the feeds every minute, and used to log the same error each
time.

`alerts.py` and `gtfs_rt_helper.py` import each other: the helper reads the
alert feed, `alerts.py` decides what one alert means for the entry. See
"Known gaps".

### What realtime changes on a sensor

**Struck trips.** A trip the feed marks CANCELED or DELETED, and a call
marked SKIPPED at the entry's origin, is no departure. The board moves on:
the departures are read again from the rows of the last static refresh
without that trip, so the next departure shown is the next one that runs,
with its own arrival, headsign and duration. A call marked NO_DATA gives
no realtime time either. A trip is struck on the service day the feed
names, since a trip cancelled today runs tomorrow under the same id.
Measured on 2026-09-15: the Dutch feed cancelled 18 % of the trips of the
hour and skipped 38 % of the calls (f8fc54c).

**Which alerts concern a journey** (`alerts.py`). The fields of one
`informed_entity` hold together, as the specification reads them: "this
trip at this stop" is about that trip alone, and an agency, a kind of
line or a direction other than the journey's make the entity about
something else (531478e, c716515). A stop matches through its station too,
since feeds derived from NeTEx publish a station and its platforms as
separate stops. A trip matches in the truncated form SNCF uses in its
alerts (`_same_trip`).

**Which alert is shown.** An alert names the departures of the board it
concerns, the next one and those listed behind it (d2afc3a). The alerts
of one end are ranked worst effect first, the ones to come after the
current ones, and five are kept (`_rank_alerts`). Each carries its periods
(66b4f66). The sentence of the sensor is taken from an alert that applies
now and concerns the departure shown: one for a day to come, or one that
names only a later departure of the board, stays in the list, never in
the sentence.

## Static feed architecture

Two separate paths: one fills the database, the other reads it.

```
Filling                              Reading

freshness.py   changed?              coordinator.py
     ↓                                    ↓
source_zip.py  fetch, keep the zip   gtfs_helper.py   get_next_departure
     ↓                                    ↓
gtfs_filter.py keep chosen routes    <file>.sqlite
     ↓
gtfs_db.py     build, swap
     ↓
<file>.sqlite
```

The map files take a third way: `gtfs_shape.py` reads shapes straight from
the kept zip.

### Refresh modes: when a source is looked at

Chosen per source on the datasource entry (`source_refresh.py`):

```
off      nothing runs by itself; the button, the update entity and the
         update service still refresh on demand
notify   one conditional request per check; on a new edition the update
         entity turns "update available", a notification is raised once per
         version, and the event gtfs2_source_update_available fires, so an
         automation can install in a window that suits the install
auto     same check, and the rebuild runs at the first check that finds a
         change
```

Only sources fetched from a url are checked: a zip source has no host.

**When.** The user picks a frequency (1 to 360 hours, 24 by default), never
a moment. Each source gets its own night slot between 03:00 and 05:59
local, derived from a hash of its file name: stable across restarts, and
sources never rebuild on the same minute. A sub-daily frequency adds passes
spaced from that anchor, so one always lands at night. A source whose last
look is overdue is caught up 10 minutes after start, past the start-up rush.

**Whether.** `probe_source` sends one conditional request built from the zip
sidecar's validators and answers unchanged, changed, unknown (the host
publishes no validators) or error. Eleven of thirteen hosts probed in
September 2026 answer 304 (550b187). On changed or unknown, the verdict is
not trusted alone: some hosts stamp a fresh Last-Modified on every answer,
so `fetch_if_new` downloads and lets the sha256 decide.

### Envelope sources: one zip, several networks

Some publishers answer one zip that holds a zip per network: SEPTA's
`gtfs_public.zip` carries `google_bus.zip` and `google_rail.zip`. Imported
as it is, no reader finds a `routes.txt`, and the flow failed three screens
later on "no routes with trips" (99dfa42).

```
source screen      zip_peek reads the url's table of contents with two
                   ranged requests (the last 64 KB, then the directory)
                   and the flow asks which network to follow
download           only that member, by its own byte range: 823 KB of a
                   21.7 MB envelope on SEPTA (99dfa42)
datasource entry   keeps the pick (inner_zip)
every refresh      asks for that member again, never for the envelope
```

A host that ignores ranges answers 200 with the whole file. The download
then takes the envelope whole and the member out of it before the feed is
checked, so the zip on disk and the hash in its sidecar are the same
whichever way it arrived. On the source screen, where nothing is picked
yet, such an envelope is kept and its networks are offered from the file.
A zip the user dropped in the folder goes through the same screen.

What comes from the remote file is bounded, since its sizes are the
host's word: a directory above 16 MB is not read, a member taken out
stops at 2 GiB like a download, and a member is inflated a chunk at a
time, never whole in memory.

### Choosing a write path

Four paths write a database. They differ because what they risk differs.

| Trigger | Path | Builds on | Swap | Why this path |
|---|---|---|---|---|
| User picks lines on the route screen | `import_routes` | scratch → real, per line | No | Append-only: existing rows are never touched, so there is nothing a reader could see half-changed. Keys are minted in the real database during the copy, so there is never a second set to remap |
| New edition (check in auto mode, update entity, button, `update_gtfs` service) | `refresh_datasource` | staging, built route by route | Yes | Every row may change; readers must see one edition or the other |
| Same, on a whole-feed source | `_refresh_whole_feed` | staging, the filtered import itself | Yes | A train or local stops sensor matches across every line, and a line the new edition brings must come in too; taking the lines from the old database never brought new ones |
| Optimise screen, `prune_datasource`, `intern_datasource` | `on_a_copy` | staging, a SQLite backup of the real one | Only if something changed | Destructive rewrites by the million plus VACUUM: on the live file they held the exclusive lock for minutes on a national feed |
| Datasource that follows no line yet | legacy `get_gtfs` | the real file, in place, in a forked process | No | Upstream's path, kept for the first import of a whole feed; see "Known defects" 1 and 2 |

**Filtering before import, not pruning after.** pygtfs pays per row: once
the whole feed is imported, the time and the disk are already spent. The
zip is cut down to the chosen routes first (`gtfs_filter.py`), written
beside the source and never into it; `routes.txt` and `agency.txt` are
copied whole so the flow keeps offering every line. When the filter cannot
run, the feed is imported whole: "slower, never wrong" (da8f4c6). On
gtfs-nl.zip, 15.1 M stop_times are filtered in 40 s to a feed pygtfs
imports in half a second, where the full import built a 2.6 GB scratch file
(`build_scratch_database`).

**Why the scratch database stays raw.** Interning it too would mean two sets
of integer keys to reconcile, and keys are local to a file: a second import
measured 31 stop keys already taken (`gtfs_db.py`).

#### Adding lines (`import_routes`)

```
scratch left by an interrupted run?   discarded first: unknown state
    ↓
zip filtered to the lines (gtfs_filter)
    ↓
scratch database (build_scratch_database), then indexed by route
    ↓
real database created from the scratch schema, if there is none
    ↓
copy_route(), one transaction per line, straight into the real database;
    network-wide tables copied with the first line only
    ↓
scratch deleted, whatever happened
```

Indexing the scratch file by route took the copy of 41 Orleans routes from
206 s to 16 s, for 9 s of indexing.

#### Refreshing a source (`refresh_datasource`)

```
real database unreadable?             stop, keep the data (see below)
real database follows no route?       legacy get_gtfs, whole feed
    ↓
new zip downloaded to <file>.zip.new, streamed, capped in size and time,
    adopted only once proven a zip
    ↓
check_source_dates set and only future dates?   stop, keep the data
    ↓
<file>.refresh.sqlite built beside the real one:
    import_routes of the followed lines, or the whole edition
    ↓
checked (see the table below)
    ↓
optimise_datasource()   intern only: everything was copied on purpose
    ↓
swap_in()               one rename over the real database
```

`routes_in` answers `None` for a file it could not read and an empty set for
a file with no trip. The two must stay apart: read as "follows nothing", an
unreadable database would go down the legacy path, which deletes the
database and the zip and rebuilds the whole network in place.

#### Shrinking a datasource (`on_a_copy`)

```
SQLite backup of the real database into <file>.refresh.sqlite
    ↓
the work, on the copy:
    optimise screen    optimise_datasource(): prune what is not followed, then intern
    prune service      prune_gtfs_datasource()
    intern service     intern_gtfs_datasource()
    ↓
swap_in(), only when the work changed something
```

Prune rebuilds each table from its own DDL rather than DELETE: DELETE pays
per row removed, a rebuild per row kept, which is the small side. Services
to keep are collected once into a keyed temp table; reaching through
`trips` instead did not come back within ten minutes on SNCF (aefaae1).

Prune refuses rather than lose data: an empty keep set, a keep set that
matches no trip, and the `"train"` marker, which needs every route kept.

## Failure and recovery

What each failure leaves, and who is told.

| Failure | Data after | User is told | Retry |
|---|---|---|---|
| Download fails or is not a zip | Old zip and database untouched; `.zip.new` removed | Refresh failed notification | Next check |
| Import of the scratch fails | Old database untouched | Refresh failed notification | Next check |
| Adding lines stops at line *k* | Lines before *k* are in; *k* and after are not | The import-done notification lists the lines that made it; nothing names the others (defect 3) | User re-picks |
| Refresh: a line fails to copy | Swap refused, old database stays | Refresh failed notification | Next check |
| Refresh: a line a sensor reads has no trip in the new edition | Swap refused, old database stays, on the route by route and the whole-feed path alike | Lines missing notification, naming them | Next check; see below |
| Refresh: every line is empty | Swap refused, the file is taken as broken | Lines missing, every line named | Next check |
| Refresh: a line nobody reads has no trip | That line is dropped, the swap goes through | Nothing | — |
| Swap cannot take the exclusive lock within 30 s | Old database stays, staging removed | Refresh failed notification | Next check |
| Rebuild fails after the zip was adopted | Zip ahead of database (`rebuild_pending`) | Refresh failed notification | Next auto check rebuilds from the kept zip first; refused again, it goes on to ask the host for a newer edition |
| Optimise screen: the copy or its swap fails | Old database stays | The screen says it failed (`generic_failure`), not "space freed" | User runs it again |
| Envelope source: the network picked is gone from a new edition | Old zip and database untouched: the envelope alone is refused as no feed | Refresh failed notification | Next check, until the publisher brings the network back |
| Refresh succeeds | New edition | Earlier failure notification dismissed | — |

**Why `rebuild_pending` exists.** The zip is adopted before the build. If the
build fails, the host asked with the zip's own validators answers
"unchanged", and the source would never be built again. The two sidecars
say it instead: the zip's records what was downloaded, the database's what
it was built from. A kept zip whose build is refused (a followed line
missing, a broken edition) would fail the same way every night, so after
that refusal the check asks the host anyway: only a newer edition can get
the source moving again.

**A line retired for good.** When the operator really removes a line a
sensor reads, the refresh is refused at every check and the source stays on
an edition that will run out. The notification names the line; the user
removes or re-targets its sensor, after which the line is no longer read
and the refresh goes through. `feed_window.py` and the diagnostic sensor
say how long the kept timetable is still good for.

**Why the swap gives up after 30 s.** Long enough for an index or an
intern, short enough not to hold the refresh behind a VACUUM of several
minutes (`SWAP_TIMEOUT`).

### After a restart or a crash

Nothing needs a recovery pass at start; each path clears what an
interrupted run of itself left before it begins:

```
<file>.import.sqlite      discarded at the start and end of every import_routes
<file>.refresh.sqlite     removed at the start and end of every refresh and on_a_copy
<file>.zip.new            overwritten by the next download
hot journal on the real   rolled back by SQLite when the next swap takes the
database                  exclusive lock, before the rename
zip ahead of database     rebuild_pending, read at the next check
```

Locks and probe states are in memory on purpose: they are re-derivable at
the next tick, and a restart only means "latest unknown" until the first
check.

## Files on disk

Everything a source owns sits in the `gtfs2` folder of the Home Assistant
configuration, named after the source's file:

```
gtfs2/
  <file>.zip                  the feed as downloaded, the only full record of it
  <file>.zip.meta.json        what the host said of that zip: final url, ETag,
                              Last-Modified, sha256, size, dates
  <file>.sqlite               the real database, the only file sensors open
  <file>.sqlite.meta.json     which edition the database was built from

  while something runs, removed when it ends or by the next run:
  <file>.zip.new              a download, adopted once proven a zip
  <file>.zip.new.inner        the network taken out of an envelope download
  <file>.import.sqlite        the scratch database of an import
  <file>.refresh.sqlite       a rebuild or a copy about to be swapped in
  *-journal                   SQLite rollback journal
  *-wal, *-shm                never created by the integration (see
                              "Concurrency"), removed defensively
```

Both sidecars are disposable caches: deleting one costs one refresh at most.
The staging and scratch files live beside the real one so a rename never
crosses a device boundary.

Map files go to `www/gtfs2/`, served to the cards as `/local/gtfs2/`:

```
www/gtfs2/
  <route>_<direction>_route.json       the line, drawn from its fullest trip
  <route>_<direction>.json             the vehicles, from the realtime positions
  <route>_<direction>_leg_<name>.json  the ride of an entry's next departure
  timetable_<name>.json                an entry's departures over the next days
```

`www/` is not where an integration usually writes. It is the one place a
card can read a file from without a custom HTTP view, which the fork does
not want to maintain. Each file is written beside its target and renamed,
so a card never reads half a file. Removing a datasource removes its
`gtfs2/` files, under its lock and off the loop. Removing an entry removes
its own leg and timetable files, and the route and vehicle files of its
line once no other entry needs them; for a train entry, the lines are read
back from the database: the trips between its two stations (0be7d37).

An entry's own files are named after the entry, folded to a file name:
accents dropped, case and punctuation folded, "Orléans" reads `orleans`.
Two names that fold to the same part would share their files, so the flow
refuses a name whose part another entry uses (642864a). The removal finds
a leg file by the ending the entry's name gives it, and checks that what
comes before that ending is a line id: a name such as "Tram 1 leg Centre"
ends the way "Centre" does, and its file is not Centre's to remove
(0be7d37). A leg file of the entry's earlier line goes when a new one is
written (6ce772e).

## Concurrency

Writers: the automatic refresh, the button, the update entity and service,
a line added from the route screen, the optimise screen, the prune and
intern services, the removal of a datasource. Readers: one coordinator per
entry, every minute. Three rules keep them apart.

**One writer per source.** Every writer takes `source_lock(hass, file)`, one
`asyncio.Lock` per source. A refresh never runs beside an import of the same
source, which would otherwise take the lines just added with it. The update
entity, the button and the scheduled check read the lock to show a rebuild
in progress or skip a tick, without waiting on it. The scheduled check's
own download, which can last half an hour and writes the same
`<file>.zip.new` a refresh stages into, runs under the lock too. A second
refresh arriving while one runs is dropped, not queued.

**Readers never see a file being written.** Writers build on a copy
(`.import`, `.refresh`) and `swap_in` puts it in place with one rename.
A rename is invisible to SQLite: a writer still in a transaction on the old
file would go on writing, and its journal, replayed against the new file,
would take it back to the old contents. So the rename happens while
holding SQLite's own exclusive lock, which every connection respects
whatever process it runs in, the forked legacy extract included. Side files
named after the real file are removed right after, so the next reader does
not replay the old file's journal into the new one.

The integration runs SQLite in its default rollback-journal mode and never
enables WAL; this is what makes the exclusive lock sufficient. Enabling WAL
would require revisiting `swap_in`.

Coordinators notice a swap through `_database_edition` and reopen the
schedule on their next cycle. Until then they keep the one already open,
which on Linux keeps reading the unlinked old file: complete, just old.

Connections wait for a busy database rather than fail: 60 s for readers
and copies (ea9ff2f: at start, entries opening one large database at once
gave up at the 5 s default and their sensors were never created), 300 s
for prune and intern.

**Nothing blocking on the event loop.** SQLite, pygtfs, zip reading and file
writes go through `hass.async_add_executor_job`. The lock is taken on the
loop and the work runs in the executor under it.

## API keys

A key is typed once and then travels far: in the url when it rides in the
query string, in headers, in the dicts debug lines print, and in the
exceptions `requests` raises, which quote the full url (`key_mask.py`,
bb4f8f6).

- One logging filter sits on the logger of every module of the
  integration and writes `*****` wherever a known key shows, tracebacks
  included. Keys are known from the entries at setup and from the flow and
  service calls on arrival. Guarding each log line one by one would miss
  the next one written.
- The flow's key screens show the same mask for a stored key: the key
  never goes back to the browser, and a mask sent back keeps the key.
- A url that carries a key is masked before it is stored or shown: the zip
  sidecar and the update entity's `source_url`.
- A key sent in a header goes to the host it was given for. `requests`
  drops only `Authorization` on a redirect to another host; `fetch` drops
  every header the caller gave, `User-Agent` and `Accept` apart (e0150bf).

## Design decisions

Each decision, why, and what was rejected.

**The zip is the source of truth.** The database can be rebuilt from the zip
at any time; shapes are read from the zip and never imported. A download
never replaces the zip before proving to be one: a moved url often keeps
answering 200 with an error page (550b187). Rejected: importing shapes,
which is most of a feed's size for one map line.

**Two databases, not one.** The file the sensors read is never the file an
import writes. Rejected: importing in place then pruning, which throws away
the pruning at each refresh and shows readers a half-built file.

**Rebuilds are atomic: build, validate, swap.** Sensors see the old complete
data or the new complete data. A failed download, import or check leaves
the current data in place.

**Adding lines does not swap.** It only appends, one transaction per line.
Building a full copy for each added line would cost the time and disk of
the whole database for a change that touches none of its existing rows.

**Source owns configuration.**

```
Datasource entry   file, url, keys, realtime feeds, refresh mode
    ↓
Journey entry      line, direction, stops, name
```

**Business logic stays outside entities.** Entities read what the domain
layer computed; the domain layer takes values, not entities, so it can be
tested without Home Assistant. Current state: see "Known gaps".

**Measure before choosing.** Performance choices name their measurement and
their feed (SNCF, IDFM, gtfs-nl, Orleans, TAO…); caps are set where the
measured cost stops paying, e.g. the headsign read over `stop_times.txt`
stops above 150 MB (eb98488).

## Verification

```
tests/            synthetic tests, run under the Home Assistant stub kept
                  in tests/, no install needed   CI: Synthetic Tests
tests_provider/   tests on real provider feeds; results.json records which
                  promises hold, xfail marks the known failures, strict
                  so a fixed case must drop its mark  CI: Provider Tests
hassfest, HACS    manifest, strings, services     CI: Validate
```

A refactor commit states which suites it ran and their counts. A change of
behaviour comes with the case that shows it.

What the suites do not reach. `sensor.py` and the config flow need Home
Assistant's entity and flow classes to import, which the stub does not
provide: neither has a test in the repository. Home Assistant's own
behaviour is read from its source, never assumed; the retry of a local
stops platform relies on Home Assistant 2026.2.3 refusing a first refresh
outside setup (1fe6d7e).

## Known gaps

What the code does not follow yet from the design above. All are meant to
be closed by the refactor, not accepted as the design. A gap is closed when
the rule it breaks can be checked by a test.

1. **`gtfs_helper.py` sits outside the layers and every layer imports it.**
   It holds the departure queries and `get_gtfs`. Closing it means moving
   the queries into the data layer, which rewrites upstream's functions in
   place, against the refactor rule above: closing it means the fork taking
   the file over, a decision the rule has so far left open.
2. **Lower layers import upper ones:** `rt_source.py`, `source_zip.py` and
   `notifications.py` import `gtfs_helper`; `gtfs_helper` imports
   `route_names`; `geojson.py` imports `gtfs_rt_helper`.
3. **Import cycles:** `alerts` ↔ `gtfs_rt_helper`; `gtfs_helper`,
   `freshness` and `rt_source`.
4. **`sensor.py` still builds much of the attributes itself**
   (`_update_attrs`).
5. **The dependency rule is not enforced.** It should become a CI check
   (for example an import-linter contract), at first with the gaps above
   listed as allowed exceptions, then with the list shrinking to none.
6. **Notifications for actionable failures** (refresh failed, lines
   missing) are persistent notifications; Home Assistant's Repairs issues
   would let the user act on them and would clear with the cause.

## Known defects

Wrong behaviours a user can meet, confirmed in the code and not fixed yet.
Unlike the gaps, they are not a matter of structure: each is to be fixed
in a commit of its own, with the case that shows it.

1. **The legacy extract outlives the source lock.** `get_gtfs` forks twice;
   the grandchild imports after the caller has returned "extracting" and
   the lock is released. Only SQLite's own locking and `check_extracting`
   keep writers apart while it runs.
2. **The legacy extract strips tables out of the kept zip in place**
   (`remove_from_zip`: shapes, transfers, translations…). A source first
   imported that way loses its shapes from the zip, against "the zip is the
   source of truth".
3. **A partial import reads as a success.** When `import_routes` stops at a
   line, the flow goes on to "reload done" as soon as one line came in, and
   the notification lists only the lines added. The lines that failed are
   named in the log only.
