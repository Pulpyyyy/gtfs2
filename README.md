# GTFS2 for Home Assistant

Departure times of your bus, tram, metro, ferry and train networks in Home Assistant. GTFS2 reads the GTFS files transport networks publish, the static timetable and the realtime feeds when the network has them, and turns them into sensors: the next departures of your commute, the departures around your home, the vehicles on a map. Everything is set up from the Home Assistant interface.

![Trams A and B and bus 40 at Gare d'Orléans, shown by the GTFS2 Live Card](https://raw.githubusercontent.com/Pulpyyyy/gtfs2-live-card/main/images/hero-light.png)

**[Documentation: the wiki](https://github.com/Pulpyyyy/gtfs2/wiki)**: [Installation](https://github.com/Pulpyyyy/gtfs2/wiki/Installation), [Data sources](https://github.com/Pulpyyyy/gtfs2/wiki/Data-sources), [Sensors](https://github.com/Pulpyyyy/gtfs2/wiki/Sensors), [Troubleshooting](https://github.com/Pulpyyyy/gtfs2/wiki/Troubleshooting)

## Features

- **[Journeys](https://github.com/Pulpyyyy/gtfs2/wiki/Journeys)**: the next departures of one line between two stops, with the arrival time and the ride time; several stops at each end, a stop on the way as a connection; the return journey made at the same time.
- **[Trains](https://github.com/Pulpyyyy/gtfs2/wiki/Train-journeys)**: pick the departure and arrival stations, then the lines; one sensor a line.
- **[Local stops](https://github.com/Pulpyyyy/gtfs2/wiki/Local-stops)**: the departures of every stop around a zone or a person.
- **[Only the lines you follow are imported](https://github.com/Pulpyyyy/gtfs2/wiki/Data-sources#importing-only-the-lines-you-follow)**: a few lines of a national network stay small and fast to set up.
- **[Timetables kept up to date](https://github.com/Pulpyyyy/gtfs2/wiki/Data-sources#keeping-the-timetable-up-to-date)**: a check each night, an update entity with an Update button, a refresh button; a refresh never breaks what works. Repairs say when an update failed or a followed line disappeared.
- **[Realtime](https://github.com/Pulpyyyy/gtfs2/wiki/Data-sources#realtime-feeds)**: delays, cancelled trips and skipped stops, vehicle positions and service alerts with their cause and effect; set once per source, downloaded once for all its sensors, read only while its lines run.
- **[Map files](https://github.com/Pulpyyyy/gtfs2/wiki/Maps-and-files)** for cards: the line drawn with its stops, the vehicles, the full ride of each listed departure, the timetable over three days.
- **[Actions](https://github.com/Pulpyyyy/gtfs2/wiki/Actions)** to list the departures, the arrivals or the stops of a trip, and to refresh or shrink a source.
- Translations: English, French, German, Spanish, Portuguese.

## The card

The [GTFS2 Live Card](https://github.com/Pulpyyyy/gtfs2-live-card) is made for this integration: departures board, realtime, alerts and a map of your lines and journeys.

## Installation

Home Assistant 2026.3 or newer.

- **With HACS**: [![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Pulpyyyy&repository=gtfs2&category=integration) or **HACS > three dots > Custom repositories**, add `https://github.com/Pulpyyyy/gtfs2` as an *Integration*; download it and restart. Releases are pre-releases for now: allow pre-releases for this repository in HACS.
- **By hand**: download `gtfs2.zip` from the latest [release](https://github.com/Pulpyyyy/gtfs2/releases), unzip it into `custom_components/gtfs2` of your configuration and restart.

Then **Settings > Devices & services > Add integration > GTFS 2**. Details, and what each screen asks: [Installation](https://github.com/Pulpyyyy/gtfs2/wiki/Installation).

### Coming from vingerha/gtfs2

Both install into the same folder, `custom_components/gtfs2`, so only one of them can be installed. This one takes over your existing GTFS2 entries and sensors and migrates them in place: nothing to set up again.

1. In HACS, remove vingerha/gtfs2 (*Remove*). Your entries in **Devices & services** stay.
2. Add this repository and download it as above.
3. Restart Home Assistant.

The migration is one way: going back to vingerha/gtfs2 afterwards is not supported.

## About this fork

[WHY_FORK.md](WHY_FORK.md) says why this fork exists, [CONCEPTS.md](CONCEPTS.md) explains its concepts in plain words, [ARCHITECTURE.md](ARCHITECTURE.md) says where each responsibility lives and why. [Performance](https://github.com/Pulpyyyy/gtfs2/wiki/Performance) gives setup time, disk, memory and CPU measured on real networks.

## Credits

This integration is a fork of [gtfs2](https://github.com/vingerha/gtfs2) by Arjan ([@vingerha](https://github.com/vingerha)). Many thanks to him: his integration is what this work grew from, and its ideas and its wiki inspired this one. Thanks also to [@joostlek](https://github.com/joostlek), [@mxbssn](https://github.com/mxbssn), [@mark1foley](https://github.com/mark1foley) for his GTFS realtime integration, and [@mordyovits](https://github.com/mordyovits) for the documentation.

Issues and ideas: [github.com/Pulpyyyy/gtfs2/issues](https://github.com/Pulpyyyy/gtfs2/issues).

## Support environmental protection

If you would like to show your appreciation for the effort put into this project, please think about supporting environmental protection efforts (as does using public transport) and consider donating to one below or any of your own choice:

- **Greenpeace**: https://www.greenpeace.org/international/donate/
- **World Wide Fund for Nature (WWF)**: https://donate.worldwildlife.org/
- **Rainforest Alliance**: https://www.rainforest-alliance.org/donate/
