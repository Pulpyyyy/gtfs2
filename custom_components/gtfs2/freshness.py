"""Ask a source's host whether the static feed changed, without downloading it.

The hosts this integration meets publish freshness almost everywhere: of
thirteen probed in September 2026, eleven sent a Last-Modified (most an ETag
too) and answered a conditional request with 304, redirects included. So
whether a refresh is worth running is a question the source itself can
answer, for the price of one small request; the download and the rebuild
only need to happen when the answer is "changed". The exceptions generate
the zip on request and send no validators at all: for those the sidecar's
hash is the only test, and it costs the download (fetch_if_new).

Everything here is synchronous on purpose, made to run in an executor job,
and touches nothing but the source's zip and its sidecar.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import homeassistant.util.dt as dt_util

from . import zip_file as zipfile
from .const import (
    CONF_API_KEY,
    CONF_API_KEY_LOCATION,
    CONF_API_KEY_NAME,
    DEFAULT_API_KEY_NAME,
)
from .key_mask import fetch, hide_keys
from .rt_source import with_query_key

_LOGGER = logging.getLogger(__name__)

PROBE_UNCHANGED = "unchanged"
PROBE_CHANGED = "changed"
PROBE_UNKNOWN = "unknown"
PROBE_ERROR = "error"


def _request_parts(data):
    """The url and headers a source is asked with, api key included."""
    url = data["url"]
    headers = {"User-Agent": "home-assistant-gtfs2"}
    key = data.get(CONF_API_KEY)
    url = with_query_key(url, data)
    if key and data.get(CONF_API_KEY_LOCATION) == "header":
        headers[data.get(CONF_API_KEY_NAME) or DEFAULT_API_KEY_NAME] = key
    return url, headers


def _comparable(validator):
    """An ETag stripped of its weak marker, so W/"x" and "x" can meet."""
    if validator and validator.startswith("W/"):
        return validator[2:]
    return validator


def _same_validator(sent, kept):
    return bool(sent) and bool(kept) and _comparable(sent) == _comparable(kept)


def probe_source(data, zip_path):
    """One cheap question to the host: has the feed changed since this zip?

    Returns {"result", "etag", "last_modified"}. The result is "unchanged",
    "changed", "unknown" when the host publishes no validators (answering
    then costs a download, see fetch_if_new), or "error" when the host
    could not be asked; the validators are the ones the host answered
    with, when it sent any, so a caller can say what the new version is.
    Nothing is downloaded and nothing on disk is touched: a "changed"
    answer is a fact about the host, and what to do with it belongs to
    the caller.
    """
    meta = source_meta(zip_path)
    conditions = {}
    if meta.get("etag"):
        conditions["If-None-Match"] = meta["etag"]
    if meta.get("last_modified"):
        conditions["If-Modified-Since"] = meta["last_modified"]
    if not conditions:
        # nothing recorded to compare with: either the sidecar is gone, and
        # one refresh will rewrite it, or the host sent no validators last
        # time and only the hash can tell
        result = PROBE_UNKNOWN if meta.get("sha256") else PROBE_CHANGED
        return {"result": result, "etag": None, "last_modified": None}

    url, headers = _request_parts(data)
    headers.update(conditions)
    try:
        response = fetch("head", url, headers=headers, allow_redirects=True,
                                 timeout=15)
        if response.status_code in (405, 501):
            # a host that refuses HEAD still answers a conditional GET with
            # 304 for free; on a real change the body is left unread
            response = fetch("get", url, headers=headers,
                                    allow_redirects=True, timeout=15,
                                    stream=True)
            response.close()
        if response.status_code == 304:
            return {"result": PROBE_UNCHANGED, "etag": meta.get("etag"),
                    "last_modified": meta.get("last_modified")}
        response.raise_for_status()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not ask %s about freshness: %s",
                        data.get("url"), ex)
        return {"result": PROBE_ERROR, "etag": None, "last_modified": None}

    etag = response.headers.get("ETag")
    last_modified = response.headers.get("Last-Modified")
    answer = {"etag": etag, "last_modified": last_modified}
    if not etag and not last_modified:
        return {"result": PROBE_UNKNOWN, **answer}
    if (_same_validator(etag, meta.get("etag"))
            or _same_validator(last_modified, meta.get("last_modified"))):
        # some hosts answer 200 to a conditional request and leave the
        # comparing to the client; same validators mean same feed
        return {"result": PROBE_UNCHANGED, **answer}
    return {"result": PROBE_CHANGED, **answer}


def probe_source_freshness(data, zip_path):
    """The probe's verdict alone, for callers with no use for the details."""
    return probe_source(data, zip_path)["result"]


def fetch_if_new(data, zip_path):
    """Download the feed and keep it only when it really is new.

    The hash decides, not the validators: this is the fallback for hosts
    that publish none, and the double check for hosts whose validators
    lie. Returns True when a new zip was swapped in, sidecar updated with
    it; False when the download matched what the zip already holds; None
    when the download failed or was not a zip. In the last two cases the
    kept zip is untouched. The caller owns the rebuild: after a True, the
    fresh feed sits in the zip and a refresh from it picks it up without
    downloading again.
    """
    url, headers = _request_parts(data)
    try:
        response = fetch("get", url, headers=headers, allow_redirects=True,
                                timeout=30, stream=True)
        response.raise_for_status()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Could not download %s: %s", data.get("url"), ex)
        return None
    staged = stage_zip(response, zip_path)
    if staged is None:
        return None
    # compared once on disk: the body is not in memory to hash beforehand
    meta = source_meta(zip_path)
    if meta.get("sha256") and file_digest(staged)[0] == meta["sha256"]:
        try:
            os.remove(staged)
        except OSError:
            pass
        return False
    adopt_zip(response, staged, zip_path)
    return True


def file_digest(path):
    """The sha256 and the size of a file, read a chunk at a time."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def source_meta_path(zip_path):
    """Where the sidecar of a source zip lives: right beside it."""
    return zip_path + ".meta.json"


def source_meta(zip_path):
    """What the sidecar remembers of the last successful download, or {}.

    The sidecar is a cache of derived facts, never primary data: deleting
    it costs at most one refresh that could have been skipped, so a missing
    or unreadable file is an empty answer, not an error.
    """
    try:
        with open(source_meta_path(zip_path), encoding="utf-8") as meta_file:
            meta = json.load(meta_file)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


# a download bigger than this is no feed anyone meant to serve: the
# largest national feeds are a few hundred megabytes zipped
FEED_MAX_BYTES = 2 * 1024 ** 3
# and one that takes longer than this is a host trickling bytes: the
# request timeout counts between two reads, never the whole transfer
FEED_DOWNLOAD_DEADLINE = 30 * 60
_CHUNK = 1024 * 1024


def _write_body(response, staged):
    """Write the body to disk as it comes, never whole in memory.

    Returns the byte count, or a sentence saying why the transfer was cut:
    too big, or too slow. A response read without stream=True has its body
    in memory already and is written the same way.
    """
    written = 0
    started = time.monotonic()
    chunks = (response.iter_content(chunk_size=_CHUNK)
              if hasattr(response, "iter_content") else [response.content])
    with open(staged, "wb") as out:
        for chunk in chunks:
            if not chunk:
                continue
            written += len(chunk)
            if written > FEED_MAX_BYTES:
                return f"is over {FEED_MAX_BYTES // 1024 ** 2} MB, cut there"
            if time.monotonic() - started > FEED_DOWNLOAD_DEADLINE:
                return f"took over {FEED_DOWNLOAD_DEADLINE // 60} minutes, cut there"
            out.write(chunk)
    return written


def stage_zip(response, zip_path):
    """Write a downloaded feed beside its target and verify it is a zip.

    A moved or renumbered url often keeps answering HTTP 200 with whatever
    now lives there: an error page, a stray protobuf, fifteen bytes of
    nothing. The kept zip is the only full record of the feed, so nothing
    replaces it before proving to be a zip. Returns the staged path, or
    None when the payload is not one.

    The body is streamed to disk, capped in size and in time: read whole
    into memory, a national feed took a gigabyte of a small machine's
    memory, and a host sending a byte at a time held the refresh, and the
    source's lock, for ever.
    """
    staged = zip_path + ".new"
    try:
        written = _write_body(response, staged)
    finally:
        close = getattr(response, "close", None)
        if close:
            close()
    reason = None
    if isinstance(written, str):
        reason = written
    elif not zipfile.is_zipfile(staged):
        reason = f"is not a zip file ({written} bytes)"
    else:
        # a zip is not yet a feed: a moved url may serve a documentation
        # archive, or an export gone empty, and swapped in that would be
        # the only record of the feed gone for good
        missing = _missing_tables(staged)
        if missing:
            reason = "is a zip but no GTFS feed, it has no " + ", ".join(missing)
    if reason:
        _LOGGER.error("The download from %s %s, keeping the current data",
                      hide_keys(response.url), reason)
        try:
            os.remove(staged)
        except OSError:
            pass
        return None
    return staged


# what makes a zip a feed the import can use at all
_REQUIRED_TABLES = ("routes.txt", "trips.txt", "stop_times.txt")


def _missing_tables(path):
    """The required tables a zip lacks, wherever the feed nested them."""
    try:
        with zipfile.ZipFile(path) as zin:
            names = {name.rsplit("/", 1)[-1] for name in zin.namelist()}
    except (OSError, zipfile.BadZipFile):
        return list(_REQUIRED_TABLES)
    return [table for table in _REQUIRED_TABLES if table not in names]


def adopt_zip(response, staged, zip_path):
    """Swap the verified download in and record what it was.

    The sidecar keeps the validators the host sent, so the next check can
    ask "did this change" for the price of one conditional request, and
    the hash, for the hosts that send no validators at all.
    """
    # the hash and the size come from the file itself: the body was
    # streamed to disk and is not held in memory any more
    digest, size = file_digest(staged)
    os.replace(staged, zip_path)
    meta = {
        # the key a query string carried stays out of the file, and out of
        # the update entity that shows this url
        "url": hide_keys(response.url),
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "sha256": digest,
        "size": size,
        "downloaded_at": dt_util.utcnow().isoformat(),
    }
    try:
        with open(source_meta_path(zip_path), "w", encoding="utf-8") as out:
            json.dump(meta, out, indent=1)
    except OSError as ex:
        _LOGGER.warning("Could not record the download of %s: %s", zip_path, ex)
