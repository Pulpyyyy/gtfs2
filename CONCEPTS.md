# Concepts

This file explains the core concepts used in the fork.

---

# Source

A source is a GTFS feed.

Example:

TAO
SNCF
Palm Bus

A source is identified by its file name.

The URL may change.

The source identity remains.

---

# Datasource

A datasource is the runtime representation of a source.

It owns:

- realtime feeds
- API keys
- refresh policy
- update state

Think:

Source
↓
Datasource

Routes and journeys consume it.

---

# Journey

A journey is a user-facing trip definition.

Examples:

Station A → Station B

Stop A → Stop B

A journey produces sensors.

---

# Edition

A feed changes over time.

Each version is called an edition.

Example:

Edition 1
↓
Edition 2
↓
Edition 3

Refresh replaces one edition with another.

---

# Freshness

Freshness answers:

    Has the source changed?

Typical signals:

- ETag
- Last-Modified
- Hash

Freshness decides whether rebuilding is necessary.

---

# Refresh

Refresh answers:

    What should happen after a change was detected?

Modes:

- Off
- Notify
- Auto

---

# Feed Window

Feed Window answers:

    How long is the timetable valid?

Examples:

- valid
- ending soon
- expired

---

# RT Window

Realtime Window answers:

    Is it useful to read realtime right now?

A line that is not operating often does not need realtime polling.

---

# ZIP

The ZIP is the source of truth.

The fork never considers the database to be authoritative.

Relationship:

ZIP
↓
Database

Never:

Database
↓
ZIP

---

# GTFS Filter

GTFS Filter creates a reduced feed.

Full Feed
↓
Filter
↓
Reduced Feed

Only the relevant routes remain.

---

# Scratch Database

Temporary database created during imports.

Purpose:

Build safely.

Not read by sensors.

---

# Staging Database

A complete candidate replacement database.

Purpose:

Validate before activation.

---

# Live Database

The only database read by sensors.

Purpose:

Serve production data.

---

# Atomic Swap

Replaces the live database with a validated staging database.

Result:

Sensors see:

- old valid data

or

- new valid data

Never half-built data.

---

# Place

A place is a user-facing travel location.

Examples:

- stop
- station
- boarding location

Places are used for journey selection.

---

# Station

A station groups related stops.

Example:

Railway station
↓
Platforms

The user interacts with the station.

The system interacts with the stops.

---

# Leg

A leg describes part of a journey.

Example:

Bus
↓
Train
↓
Metro

The complete journey contains multiple legs.

---

# Timetable

A timetable contains departures over time.

The timetable export is used by advanced consumers to reason about journeys.

---

# Direction Repair

Some providers publish incorrect directions.

Direction Repair attempts to reconstruct the intended direction from actual stop ordering.

Valid directions are preserved.

Incorrect directions may be repaired.

---

# Provider Verification

A provider test verifies behaviour against a real-world feed.

Goal:

Detect regressions before users do.

Principle:

Every recurring bug should eventually become a regression test.