"""Read one shape out of a feed's zip, for the line drawn on a map.

shapes.txt is never imported: pygtfs pays per row and the file is the bulk
of a regional feed (26 MB of the Zou zip, 244 MB of the Dutch national one)
for points no departure query ever reads. What a map needs is one polyline
per line and direction, a few hundred points, and the zip the integration
keeps beside the database still holds every one of them. So the shape of
the trip that stands for the line is read from the zip when that file is
written, in one streaming pass, and nothing of it reaches the database.

Measured on the TAO feed: a shape of tram A is 150 points, 7 kB of csv,
and the pass over its 25,000-row shapes.txt takes well under a second.

Everything here reads a zip: no Home Assistant, no pygtfs, no database,
which is what keeps it loadable by the test harness on its own.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile

from .gtfs_filter import _member

_LOGGER = logging.getLogger(__name__)


def trip_shape_id(zip_path, trip_id):
    """The shape_id the zip's own trips.txt gives a trip.

    The number and the points have to come from one edition. A shape_id is
    the publisher's to reuse: IDFM renumbers its shapes at every export, and
    shp_1_162 was a metro 6 shape on 2026-09-19 and a metro 9 one on
    2026-09-27. The database is built from the zip, but not at the same
    moment: a refresh adopts the zip first and builds after, and a shape_id
    read from the database then points into the wrong edition.

    None when the zip has no trips.txt, or no row for that trip (an edition
    whose trip ids moved on), or names no shape for it.
    """
    if not trip_id or not zip_path:
        return None
    trip_id = str(trip_id)
    try:
        with zipfile.ZipFile(zip_path) as zin:
            # wherever the feed nested it, as the import finds it
            member = _member(zin, "trips.txt")
            if member is None:
                return None
            with zin.open(member) as raw:
                reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                header = next(reader, None)
                if header is None:
                    return None
                columns = {name.strip(): index for index, name in enumerate(header)}
                if "trip_id" not in columns or "shape_id" not in columns:
                    return None
                c_trip, c_shape = columns["trip_id"], columns["shape_id"]
                width = max(c_trip, c_shape) + 1
                for row in reader:
                    if len(row) >= width and row[c_trip] == trip_id:
                        # trip_id is the table's key: the first row is the one
                        return row[c_shape].strip() or None
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, csv.Error) as ex:
        _LOGGER.warning("Could not read the shape of trip %s from %s: %s", trip_id, zip_path, ex)
    return None


def read_shape(zip_path, shape_id):
    """The points of one shape, as [lon, lat] pairs in shape_pt_sequence
    order, geojson's way round.

    None when there is nothing to draw from: no zip, no shapes.txt in it
    (the historic import strips it in place, the SNCF never ships one), a
    shapes.txt without the required columns, or no row of that shape. A
    caller draws the stops joined up in that case, as it did before.
    """
    if not shape_id or not zip_path:
        return None
    shape_id = str(shape_id)
    points = []
    try:
        with zipfile.ZipFile(zip_path) as zin:
            # wherever the feed nested it, as the import finds it
            member = _member(zin, "shapes.txt")
            if member is None:
                return None
            with zin.open(member) as raw:
                # utf-8-sig: some editors write the header behind a BOM
                reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
                header = next(reader, None)
                if header is None:
                    return None
                columns = {name.strip(): index for index, name in enumerate(header)}
                try:
                    c_id = columns["shape_id"]
                    c_lat = columns["shape_pt_lat"]
                    c_lon = columns["shape_pt_lon"]
                    c_seq = columns["shape_pt_sequence"]
                except KeyError:
                    _LOGGER.warning("shapes.txt of %s lacks a required column: %s", zip_path, header)
                    return None
                width = max(c_id, c_lat, c_lon, c_seq) + 1
                for row in reader:
                    if len(row) < width or row[c_id] != shape_id:
                        continue
                    try:
                        points.append((int(row[c_seq]), float(row[c_lon]), float(row[c_lat])))
                    except ValueError:
                        # one bad row does not lose the shape, the others draw it
                        continue
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, csv.Error) as ex:
        _LOGGER.warning("Could not read shape %s from %s: %s", shape_id, zip_path, ex)
        return None
    if not points:
        return None
    # the feed may list the points in any order: the sequence is the order
    points.sort(key=lambda point: point[0])
    return [[lon, lat] for _, lon, lat in points]
