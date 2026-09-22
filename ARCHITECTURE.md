# GTFS2 Architecture

## Overview

GTFS2 is organised in five layers, plus a query module and a few shared
modules that every layer uses:

```text
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

The layers describe where each responsibility belongs. The imports do not
follow them strictly yet: see "Known gaps" at the end.

## Glossary

```text
source        a GTFS feed as the integration knows it, identified by its file
              name; its url can change, the name stays
datasource    what a source becomes on disk (its zip and database) and in Home
              Assistant (its datasource entry)
edition       one version of a source's feed; a refresh replaces one edition
              with the next
line, route   a GTFS route; the code says route, the screens say line
journey       a sensor's trip on one line, from an origin to a destination, in
              one direction
local stops   an entry that follows a person and lists the departures of the
              stops around them
real, scratch the database sensors read, and the throwaway one an import
              builds (see "Import architecture")
window        the hours a source's realtime feeds are read, derived from its
              timetable (rt_window.py)
```

## 1. Home Assistant layer

Files:

```text
__init__.py
coordinator.py
sensor.py
update.py
switch.py
button.py
```

Purpose:
- Set up and unload entries, register the services (`__init__.py`)
- Run the refresh cycle of each journey entry (`GTFSUpdateCoordinator`) and
  of each local stops entry (`GTFSLocalStopUpdateCoordinator`)
- Expose the entities: departure sensors, the update entity and the refresh
  button of a source, the realtime switch of a datasource
- Build the sensor attributes, with the groups filled by
  `departure_attributes.py`

The coordinator holds no SQL: it reads the timetable through `gtfs_helper`
and the realtime through `gtfs_rt_helper`, and hands the fork's own steps to
`refresh_steps.py` and `exports.py`.

## 2. Config flow layer

Files:

```text
config_flow.py     the flow itself, composed of the screen classes below
flow_source.py     SourceScreens: where the timetable comes from
flow_reload.py     ReloadScreens: load lines into a datasource, shrink it
flow_journey.py    JourneyScreens: direction, sensor name, mirror journey
flow_train.py      TrainScreens: arrival station and sensor
flow_options.py    OptionsScreens: realtime feeds and static refresh of a source
```

Purpose:
- Collect user input
- Create datasource entries and journey entries
- Start imports and watch them after the window closes (the outcome is told
  through `notifications.py`)

## 3. Domain services layer

Files:

```text
alerts.py                what a service alert means for one sensor
departure_attributes.py  the attribute groups the fork adds to a sensor
refresh_steps.py         next service date, trips struck by the realtime
route_names.py           line labels, lines a feed declares
stations.py              train entries: stations instead of stops
exports.py               which map files a refresh writes, and when
```

Purpose:
- Alert matching
- Route naming
- Station handling
- Sensor attributes
- Export scheduling (the ride of the next departure, stop by stop, is
  `export_leg`; the writers themselves live in `geojson.py`)

## 4. Data management layer

Files:

```text
gtfs_db.py            everything that opens a database file directly
gtfs_filter.py        cut a zip down to chosen routes before any import
direction_repair.py   repair trip direction_id after import
gtfs_shape.py         read one shape out of the zip (shapes.txt is never imported)
geojson.py            the files written under www/gtfs2 for a map card
feed_window.py        how long the kept timetable is good for
```

Purpose:
- Database lifecycle: real and scratch files, copy, prune, intern, swap
- Feed filtering
- Direction repair
- Shapes and GeoJSON files
- Feed validity

## 5. Source & feed layer

Files:

```text
rt_source.py        the datasource entries, owning the realtime feeds and keys
source_zip.py       the zip beside a datasource: fetched, kept, refreshed
freshness.py        ask the host whether the feed changed, without downloading
source_refresh.py   automatic refresh of the static feeds, per source and mode
rt_window.py        when the realtime feeds are worth reading
```

Purpose:
- Datasource ownership
- Feed acquisition and refresh
- Version detection
- Realtime scheduling

## Config entries

Three kinds of entry, told apart in `async_setup_entry`:

```text
datasource   data["kind"] == "datasource"   one per source
local stops  data["device_tracker_id"] set  one per followed person
journey      anything else                  one per sensor (bus or train)
```

**Datasource entry.** Runs no coordinator. It carries what belongs to the
whole source: url and key of the static feed, the realtime feeds and their
keys, the refresh mode. Its platforms are `DATASOURCE_PLATFORMS`: the
update entity and the refresh button of the source, the switch that
silences its realtime, and two diagnostic sensors (whether realtime runs,
how long the timetable is good for). It also arms the scheduled look at
the source's host (`async_arm_source_check`).

Datasource entries are created at startup from the journey entries already
there (`async_bootstrap_datasource_entries`), grouped by file. Every edit on
one is written back onto the journey entries of the source
(`async_mirror_rt_to_entries`), so a downgrade to a version without
datasource entries still finds current values.

**Journey entry.** Gets a `GTFSUpdateCoordinator`, kept on
`entry.runtime_data`, and one departure sensor. A bus journey names its
line, direction and stops; a train journey names stations and stores
`route = "train"`, a marker rather than a route id.

**Local stops entry.** Gets a `GTFSLocalStopUpdateCoordinator` on
`entry.runtime_data`, and one sensor per stop around the person.

Each cycle, a coordinator asks `rt_feed_config` for its realtime settings:
the datasource entry's when it exists, the entry's own options otherwise.
An edit on the datasource entry reaches every sensor of the source within
a minute, without a reload.

## A refresh cycle

`GTFSUpdateCoordinator._async_update_data`, every minute:

```text
1. source still being unpacked?        keep the previous data, flag it, stop
2. static refresh due?                 the entry's refresh interval passed,
                                       or the departure shown has left
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
     drop_struck_trips                 a cancelled or skipped trip goes,
                                       the next one takes its place
   realtime off or paused:             delays and alerts emptied, never
                                       carried over from another moment
4. export_leg                          when the static ran or realtime was read
5. _read_records                       the rows the sensor describes the
                                       departure with
```

The schedule itself is opened by `schedule_for`, reopened only when the
database file changed (see "Concurrency").

## Realtime architecture

```text
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

`alerts.py` and `gtfs_rt_helper.py` import each other: the helper reads the
alert feed, `alerts.py` decides what one alert means for the entry.

## Static feed architecture

Two separate paths: one fills the database, the other reads it.

```text
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

## Import architecture

Each datasource has two database files:

```text
<file>.sqlite         real: only the followed routes, the only file sensors open
<file>.import.sqlite  scratch: raw pygtfs output, deleted when the import ends
```

### Adding lines (`import_routes`, from the route screen)

```text
zip filtered to the lines (gtfs_filter)
    ↓
scratch database (build_scratch_database)
    ↓
copy_route(), one transaction per line, straight into the real database
    ↓
scratch deleted
```

No swap: the real database is either untouched or has gained lines. The keys
are minted in the real database during the copy, so there is never a second
set to remap.

### Refreshing a source (`refresh_datasource`)

```text
new zip downloaded beside the old one, adopted once proven a zip
    ↓
<file>.refresh.sqlite built beside the real one
    (import_routes of the followed lines, or the whole edition for a
     source a train or local stops sensor reads whole)
    ↓
checked: every line a sensor reads still has trips
    ↓
optimise_datasource()   intern
    ↓
swap_in()               one rename over the real database
```

### Shrinking a datasource (optimise screen, prune and intern services)

```text
backup of the real database (on_a_copy)
    ↓
the work, on the copy:
    optimise screen    optimise_datasource(): prune what is not followed, then intern
    prune service      prune_gtfs_datasource()
    intern service     intern_gtfs_datasource()
    ↓
swap_in(), only when the work changed something
```

## Files on disk

Everything a source owns sits in the `gtfs2` folder of the Home Assistant
configuration, named after the source's file:

```text
gtfs2/
  <file>.zip                  the feed as downloaded, the only full record of it
  <file>.zip.meta.json        what the host said of that zip: ETag, Last-Modified
  <file>.sqlite               the real database, the only file sensors open
  <file>.sqlite.meta.json     which edition the database was built from

  while something runs, removed when it ends or by the next run:
  <file>.zip.new              a download, adopted once proven a zip
  <file>.import.sqlite        the scratch database of an import
  <file>.refresh.sqlite       a rebuild or a copy about to be swapped in
  *-journal, -wal, -shm       SQLite side files
```

The zip and the database carry separate records on purpose. When the zip
is ahead of the database, a rebuild was started and did not finish
(`rebuild_pending`): the next refresh rebuilds from the kept zip instead
of downloading it again.

The map files go to `www/gtfs2/`, served to the cards as `/local/gtfs2/`:

```text
www/gtfs2/
  <route>_<direction>_route.json       the line, drawn from its fullest trip
  <route>_<direction>.json             the vehicles, from the realtime positions
  <route>_<direction>_leg_<name>.json  the ride of an entry's next departure
  timetable_<name>.json                an entry's departures over the next days
```

Each is written beside its target and renamed, so a card never reads half
a file. Removing a datasource removes its `gtfs2/` files
(`remove_datasource`). Removing an entry removes its own leg and timetable
files, and the route and vehicle files of its line once no other entry on
that line needs them.

## Concurrency

Several writers can reach the same database: the automatic refresh, a line
added from the route screen, the optimise screen, the prune and intern
services, the removal of a datasource. Many readers do too: one coordinator
per entry, every minute. Three rules keep them apart.

**One writer per source.** Every writer takes `source_lock(hass, file)`,
one `asyncio.Lock` per source. A refresh never runs beside an import of
the same source, which would otherwise take the lines just added with it.
The update entity, the refresh button and the scheduled check read the
lock to show a rebuild in progress, or to skip a tick, without waiting on
it.

**Readers never see a file being written.** Writers build on a copy
(`.import`, `.refresh`) and `swap_in` puts it in place with one rename,
taken under SQLite's own exclusive lock so no transaction still open on
the old file can replay its journal on the new one. The swap waits
`SWAP_TIMEOUT` (30 s) for a writer to finish, then gives up and leaves the
current data in place. Coordinators notice the swap through the file's
inode, size and last write (`_database_edition`) and reopen the schedule
on their next cycle. Until then they keep the one already open.

**Nothing blocking on the event loop.** SQLite, pygtfs, zip reading and
file writes go through `hass.async_add_executor_job`. The one-lock-per-source
rule is what makes that safe: the lock is taken on the loop, the work runs
in the executor under it.

## Design principles

### Source owns configuration

```text
Datasource entry   file, url, keys, realtime feeds, refresh mode
    ↓
Journey entry      line, direction, stops, name
```

### Business logic stays outside entities

### Rebuilds are atomic

```text
Build copy
    ↓
Validate
    ↓
Swap
```

The sensors only ever see the old complete data or the new complete data.
A failed download, import or check leaves the current data in place.

### ZIP is the source of truth

```text
ZIP (kept beside the database)
 ↓
Database (only what is followed)
```

The database can be rebuilt from the zip at any time; shapes are read from
the zip and never imported.

## Known gaps

What the code does not follow yet from the layers above. All four are
meant to be closed by the refactor, not accepted as the design:

- `gtfs_helper.py` is outside the layers and imported from all of them. It
  holds the departure queries and `get_gtfs`, the legacy full extract still
  used for a source that follows no line yet.
- Lower layers import upper ones: `rt_source.py`, `source_zip.py` and
  `notifications.py` import `gtfs_helper`, `gtfs_helper` imports
  `route_names`, `geojson.py` imports `gtfs_rt_helper`.
- Import cycles: `alerts` and `gtfs_rt_helper`; `gtfs_helper`, `freshness`
  and `rt_source`.
- `sensor.py` still builds much of the attributes itself (`_update_attrs`),
  so "business logic stays outside entities" is a goal, not the current state.
