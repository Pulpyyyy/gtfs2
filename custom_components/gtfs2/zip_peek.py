"""Read a remote zip's table of contents, and take one member out of it.

Some publishers answer a zip that holds other zips, one per network:
SEPTA's gtfs_public.zip carries google_bus.zip and google_rail.zip. Picking
one of them used to mean downloading the whole envelope and unpacking it by
hand, when a zip keeps its table of contents at the end of the file and
HTTP can ask for the end of a file alone.

So the envelope is read, not downloaded: one ranged request brings back its
last 64 KB, a second the directory that names every member, and the feed
the user chose is fetched by its own byte range. Measured on SEPTA, 823 KB
of a 21.7 MB envelope, 3.8 per cent, and the same three requests would save
as much of a national feed as it is big.

A host that ignores Range answers 200 with the whole file, which is exactly
what this is avoiding, so the body is never read in that case and the
caller falls back to the plain download it would have done anyway. Members
are streamed through the decompressor, never held whole in memory, like
every other download here.

Called from source_zip (the config flow's source step) and from freshness
(the automatic refresh, which re-fetches the member the source was built
from). The local counterparts serve the same screens for a zip the user
dropped in the folder themselves.
"""
from __future__ import annotations

import logging
import os
import struct
import zlib

from . import zip_file as zipfile
from .key_mask import fetch, hide_keys

_LOGGER = logging.getLogger(__name__)

# the end of central directory record, plus room for a zip comment
_TAIL = 65536
_CHUNK = 64 * 1024
# a member taken out of an envelope is held to what a download is
# (freshness.FEED_MAX_BYTES), and a directory to what a few thousand
# members take: the sizes come from the remote file, which may lie
_MEMBER_MAX = 2 * 1024 ** 3
_DIRECTORY_MAX = 16 * 1024 ** 2
# what the directory says a member is stored with
_STORED, _DEFLATED = 0, 8


def _ranged(url, headers, span, stream=False):
    """One ranged GET, or (None, response) when the host ignored the range.

    A 200 here means the host is sending the whole file: the body is left
    unread and the connection closed, because reading it is the download
    this module exists to avoid.
    """
    asked = dict(headers or {})
    asked["Range"] = f"bytes={span}"
    response = fetch("get", url, headers=asked, allow_redirects=True,
                     timeout=30, stream=True)
    response.raise_for_status()
    if response.status_code != 206:
        _LOGGER.debug("%s ignored a range request, answering %s",
                      hide_keys(url), response.status_code)
        response.close()
        return None, response
    if stream:
        return response, response
    body = response.content
    response.close()
    return body, response


def _directory(url, headers):
    """{name: (offset, compressed size, method)} for a remote zip's members.

    Returns an empty mapping when the host refuses ranges, when the tail is
    not a zip, or when the envelope is large enough to carry a zip64
    directory, which this does not read: in every one of those the caller
    downloads the file as it always did.
    """
    tail, response = _ranged(url, headers, f"-{_TAIL}")
    if tail is None:
        return {}, response
    end = tail.rfind(b"PK\x05\x06")
    if end < 0:
        _LOGGER.debug("No zip directory at the end of %s", hide_keys(url))
        return {}, response
    size, offset = struct.unpack("<II", tail[end + 12:end + 20])
    if size > _DIRECTORY_MAX:
        _LOGGER.debug("%s announces a %s byte directory, not read", hide_keys(url), size)
        return {}, response
    if 0xFFFFFFFF in (size, offset):
        _LOGGER.debug("%s carries a zip64 directory, reading it whole",
                      hide_keys(url))
        return {}, response
    listing, response = _ranged(url, headers, f"{offset}-{offset + size - 1}")
    if listing is None:
        return {}, response
    members = {}
    at = 0
    while at < len(listing) and listing[at:at + 4] == b"PK\x01\x02":
        method, = struct.unpack("<H", listing[at + 10:at + 12])
        packed, = struct.unpack("<I", listing[at + 20:at + 24])
        name_len, extra_len, comment_len = struct.unpack(
            "<HHH", listing[at + 28:at + 34])
        where, = struct.unpack("<I", listing[at + 42:at + 46])
        name = listing[at + 46:at + 46 + name_len].decode("utf-8", "replace")
        members[name] = (where, packed, method)
        at += 46 + name_len + extra_len + comment_len
    return members, response


def _zips_among(names):
    """The members that are zips themselves, in the order a reader expects."""
    return sorted(name for name in names if name.lower().endswith(".zip"))


def inner_zips(url, headers):
    """The zips a remote zip holds, or [] when it is a feed or unreadable.

    [] is the answer that changes nothing: the caller downloads the url the
    way it always has. A non-empty list means the url is an envelope and
    the user has a network to pick.
    """
    try:
        members, _ = _directory(url, headers)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("Could not read the directory of %s: %s", hide_keys(url), ex)
        return []
    if not members or any(
            name.rsplit("/", 1)[-1] == "routes.txt" for name in members):
        return []
    return _zips_among(members)


class _MemberResponse:
    """One member of a remote zip, shaped like the response that carried it.

    stage_zip streams whatever it is handed and adopt_zip records the url
    and validators it answered with, so a member is written and its
    freshness remembered by the same code as a whole download. The
    validators are the envelope's, which is the right thing to watch: the
    member changes when the envelope does.
    """

    def __init__(self, response, packed, method):
        self._response = response
        self._packed = packed
        self._method = method
        self.url = response.url
        self.headers = response.headers
        self.status_code = 200

    def raise_for_status(self):
        """Already raised on the ranged request that built this."""

    def iter_content(self, chunk_size=_CHUNK):
        """The member's bytes, inflated as they arrive."""
        left = self._packed
        unzip = zlib.decompressobj(-15) if self._method == _DEFLATED else None
        for chunk in self._response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            # the range was asked for exactly, but a host may send more
            chunk = chunk[:left]
            left -= len(chunk)
            if not unzip:
                yield chunk
            else:
                # inflated a chunk at a time: a few compressed bytes can
                # stand for gigabytes, which must not land in memory at once
                out = unzip.decompress(chunk, chunk_size)
                yield out
                while unzip.unconsumed_tail:
                    yield unzip.decompress(unzip.unconsumed_tail, chunk_size)
            if left <= 0:
                break
        if unzip:
            yield unzip.flush()

    def close(self):
        self._response.close()


def open_member(url, headers, name):
    """A response whose body is that member of the remote zip, or None.

    None when the member is gone from the envelope, when the host refuses
    ranges, or when it is stored in a way this does not read: the caller
    then downloads the envelope whole and takes the member out of the file.
    """
    try:
        members, _ = _directory(url, headers)
        found = members.get(name)
        if found is None:
            _LOGGER.warning("%s no longer holds %s", hide_keys(url), name)
            return None
        where, packed, method = found
        if method not in (_STORED, _DEFLATED):
            _LOGGER.warning("%s stores %s in an unknown way (%s)",
                            hide_keys(url), name, method)
            return None
        # the local header repeats the name and may carry a different extra
        # field, so its own lengths say where the bytes really start
        head, _ = _ranged(url, headers, f"{where}-{where + 29}")
        if head is None or head[:4] != b"PK\x03\x04":
            return None
        name_len, extra_len = struct.unpack("<HH", head[26:30])
        first = where + 30 + name_len + extra_len
        body, response = _ranged(
            url, headers, f"{first}-{first + packed - 1}", stream=True)
        if body is None:
            return None
        return _MemberResponse(response, packed, method)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not take %s out of %s: %s", name, hide_keys(url), ex)
        return None


def inner_zips_in_file(path):
    """The zips a zip on disk holds, for an envelope the user supplied."""
    try:
        with zipfile.ZipFile(path) as zin:
            names = zin.namelist()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("Could not read %s: %s", path, ex)
        return []
    if any(name.rsplit("/", 1)[-1] == "routes.txt" for name in names):
        return []
    return _zips_among(names)


def member_out_of(staged, name):
    """Leave the member alone in a staged file that holds the envelope.

    The path taken when a host that used to answer ranges stops: the
    envelope was downloaded whole, and the network the source was built
    from is still the one to keep. Returns the staged path either way, so
    a file that already is the feed goes through untouched.
    """
    if not name or name not in set(inner_zips_in_file(staged)):
        return staged
    taken = staged + ".inner"
    if not extract_member(staged, name, taken):
        return staged
    os.replace(taken, staged)
    _LOGGER.debug("Took %s out of the downloaded envelope", name)
    return staged


def extract_member(path, name, staged):
    """Write one member of a zip on disk to staged. True when it landed;
    on a failure nothing is left behind, half a member being no feed."""
    try:
        with zipfile.ZipFile(path) as zin:
            with zin.open(name) as member, open(staged, "wb") as out:
                written = 0
                while True:
                    chunk = member.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _MEMBER_MAX:
                        raise ValueError(f"over {_MEMBER_MAX // 1024 ** 2} MB, cut there")
                    out.write(chunk)
        return True
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Could not take %s out of %s: %s", name, path, ex)
        try:
            os.remove(staged)
        except OSError:
            pass
        return False
