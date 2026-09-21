"""The lines a source zip declares, read by the one routes.txt reader.

get_routes_in_zip had a reader of its own, beside the one the flow uses;
it goes through that one now. A quoted field holding a line break and a
byte order mark leading the file are read as the csv module means them.
"""
from __future__ import annotations

import zipfile

import ha_stub

route_names = ha_stub.load("route_names")

ROUTES = ('﻿route_id,route_short_name,route_long_name,route_type\r\n'
          'R1,1,"Gare\r\nLac",3\r\n'
          'R2,2,Stade,3\r\n')


def test_every_line_of_the_zip(tmp_path):
    with zipfile.ZipFile(tmp_path / "src.zip", "w") as zout:
        zout.writestr("routes.txt", ROUTES.encode("utf-8"))
    assert route_names.get_routes_in_zip(str(tmp_path), "src") == {"R1", "R2"}


def test_no_zip_cannot_tell(tmp_path):
    assert route_names.get_routes_in_zip(str(tmp_path), "src") == set()
