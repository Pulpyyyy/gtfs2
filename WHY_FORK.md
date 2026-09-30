# Why this fork exists

This fork was not created to add features for the sake of adding features.

It exists because the same classes of problems kept appearing across real GTFS providers, over several years of usage, issues and support requests.

The goal is therefore:

    Fix recurring problem families once,
    instead of fixing each occurrence separately.

---

## The core idea

The upstream project works well for many feeds.

However, GTFS providers are highly variable:

- some omit fields
- some publish inconsistent direction_id values
- some rebuild trip_ids every day
- some expose enormous feeds
- some update their feeds daily
- some publish broken or incomplete realtime data

A provider that works today can change tomorrow.

The fork therefore focuses on mechanisms that absorb variability rather than handling providers one by one.

---

# Problem: duplicated realtime configuration

Current situation:

Route A
 └─ RT URL

Route B
 └─ RT URL

Route C
 └─ RT URL

The same URL, API key and feed may be configured and downloaded multiple times.

Solution:

Datasource

A datasource owns:

- realtime feeds
- API keys
- refresh policy
- source state

Routes consume the datasource.

Benefits:

- one configuration
- one download
- one state
- easier maintenance

---

# Problem: rebuilding unchanged feeds

Current situation:

Download
↓
Rebuild
↓
Nothing changed

Repeated forever.

Solution:

Freshness

The fork checks:

- ETag
- Last-Modified
- Hash fallback

before rebuilding anything.

Benefits:

- less network traffic
- less CPU
- less disk activity
- faster updates

---

# Problem: very large GTFS feeds

Many users only follow:

- a few lines
- a few stops
- a few journeys

Yet the whole network is imported.

Solution:

GTFS Filter

The fork imports only the routes that are actually used.

The ZIP remains the source of truth.

The database becomes an optimized working copy.

Benefits:

- smaller databases
- faster imports
- lower memory usage

---

# Problem: destructive refreshes

Current situation:

Live database
↓
Rebuild in place

If something fails, readers may see incomplete or invalid data.

Solution:

Two Database Flow

scratch
↓
staging
↓
live

The database used by sensors is never written directly.

Benefits:

- atomic refresh
- rollback behaviour
- safer updates

---

# Problem: inconsistent feeds

Many providers ship:

- missing directions
- wrong directions
- inconsistent stops
- malformed realtime feeds

Solution:

Repair and validation layers

Examples:

- direction_repair.py
- feed validation
- realtime validation

Benefits:

- more feeds work correctly
- fewer provider-specific fixes

---

# Problem: recurring regressions

A fix that works for one provider can break another.

Solution:

Provider verification

The fork validates behaviour against real providers and regression scenarios.

Goal:

Every bug should become a test.

---

# Principles

The fork follows a few simple rules:

1. Prefer fixing a problem family to fixing one occurrence.
2. Keep the ZIP as the source of truth.
3. Never destroy working data to build new data.
4. Measure before optimizing.
5. Push complexity into the software rather than into user workflows.

---

# Non-goals

The fork is not trying to:

- replace GTFS
- replace pygtfs
- solve every possible provider issue
- become a route planner

The goal is simply to make GTFS usage more robust across real-world providers.