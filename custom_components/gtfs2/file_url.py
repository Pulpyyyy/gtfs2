"""Read a file:// url the way requests reads an http one.

Every source is fetched from its url, and a feed on this machine has one
too: file:///config/gtfs2/tao.zip. Answered by requests itself, through
this transport adapter on the session fetch opens, a feed on disk goes
the way a hosted one does: the check's cheap question (a HEAD carrying
If-Modified-Since, 304 while the file is the one the sidecar recorded),
the streamed download with its size and time caps, the hash that says
whether the bytes really changed.

The file's own modification time is its Last-Modified, and the question
is whether the file is still the one recorded: 304 for that very time,
any other one is another file. Answered as "no newer than", the way an
http host reads the header, a new edition copied in with the older time
it was published at read as the old one. There is no ETag: a file
rewritten with the same bytes reads as changed to the question, and the
download's hash then finds it is not, as for a host that stamps a new
date on every answer.
"""
from __future__ import annotations

from collections.abc import Mapping
import email.utils
import io
import os
import pathlib
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

FILE_SCHEME = "file://"


def file_url(path: str) -> str:
    """The file:// url of a path on this machine."""
    return pathlib.Path(path).absolute().as_uri()


def url_path(url: str) -> str:
    """The path a file:// url names."""
    return url2pathname(urlsplit(url).path)


def _written_at(mtime: float, since: str) -> bool:
    """Whether a file last written at mtime was written at the moment an
    If-Modified-Since date names, to the second as http dates are; a date
    that cannot be read asks for the file."""
    try:
        asked = email.utils.parsedate_to_datetime(since).timestamp()
    except (TypeError, ValueError, IndexError):
        return False
    return int(mtime) == int(asked)


class _FileBody(io.FileIO):
    """A file read as a response body. requests lets go of a body read to
    its end by releasing its connection, not by closing it: left open, the
    file could not be replaced on Windows, where a source fed by its own
    kept zip swaps the copy in over it."""

    def release_conn(self) -> None:
        self.close()


class FileAdapter(BaseAdapter):
    """Answer a file:// request as a host would: 200 with the file, 304
    when it is the one recorded at the time asked, 404 when there is none."""

    def send(self, request: requests.PreparedRequest, stream: bool = False,
             timeout: Any = None, verify: Any = True, cert: Any = None,
             proxies: Mapping[str, str] | None = None) -> requests.Response:
        response = requests.Response()
        response.request = request
        response.url = request.url or ""
        path = url_path(response.url)
        try:
            stat = os.stat(path)
        except OSError as ex:
            response.status_code = 404
            response.reason = ex.strerror or "Not Found"
            return response
        response.headers = CaseInsensitiveDict({
            "Last-Modified": email.utils.formatdate(stat.st_mtime, usegmt=True),
            "Content-Length": str(stat.st_size),
        })
        since = request.headers.get("If-Modified-Since")
        if since and _written_at(stat.st_mtime, str(since)):
            response.status_code = 304
            response.reason = "Not Modified"
            return response
        response.status_code = 200
        response.reason = "OK"
        if request.method == "HEAD":
            return response
        if stream:
            # read as it is consumed, and closed with the response
            response.raw = _FileBody(path, "rb")
        else:
            with open(path, "rb") as handle:
                response.raw = io.BytesIO(handle.read())
        return response

    def close(self) -> None:
        """Nothing is held between two requests."""
