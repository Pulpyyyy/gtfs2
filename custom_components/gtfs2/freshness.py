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
import logging

import requests

from .const import (
    CONF_API_KEY,
    CONF_API_KEY_LOCATION,
    CONF_API_KEY_NAME,
    DEFAULT_API_KEY_NAME,
)
from .gtfs_helper import adopt_zip, source_meta, stage_zip

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
    if key and data.get(CONF_API_KEY_LOCATION) == "query_string":
        url = url + "?" + (data.get(CONF_API_KEY_NAME) or DEFAULT_API_KEY_NAME) + "=" + key
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


def probe_source_freshness(data, zip_path):
    """One cheap question to the host: has the feed changed since this zip?

    Returns "unchanged", "changed", "unknown" when the host publishes no
    validators (answering then costs a download, see fetch_if_new), or
    "error" when the host could not be asked. Nothing is downloaded and
    nothing on disk is touched: a "changed" answer is a fact about the
    host, and what to do with it belongs to the caller.
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
        return PROBE_UNKNOWN if meta.get("sha256") else PROBE_CHANGED

    url, headers = _request_parts(data)
    headers.update(conditions)
    try:
        response = requests.head(url, headers=headers, allow_redirects=True,
                                 timeout=15)
        if response.status_code in (405, 501):
            # a host that refuses HEAD still answers a conditional GET with
            # 304 for free; on a real change the body is left unread
            response = requests.get(url, headers=headers,
                                    allow_redirects=True, timeout=15,
                                    stream=True)
            response.close()
        if response.status_code == 304:
            return PROBE_UNCHANGED
        response.raise_for_status()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not ask %s about freshness: %s",
                        data.get("url"), ex)
        return PROBE_ERROR

    etag = response.headers.get("ETag")
    last_modified = response.headers.get("Last-Modified")
    if not etag and not last_modified:
        return PROBE_UNKNOWN
    if (_same_validator(etag, meta.get("etag"))
            or _same_validator(last_modified, meta.get("last_modified"))):
        # some hosts answer 200 to a conditional request and leave the
        # comparing to the client; same validators mean same feed
        return PROBE_UNCHANGED
    return PROBE_CHANGED


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
        response = requests.get(url, headers=headers, allow_redirects=True,
                                timeout=30)
        response.raise_for_status()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Could not download %s: %s", data.get("url"), ex)
        return None
    meta = source_meta(zip_path)
    if (meta.get("sha256")
            and hashlib.sha256(response.content).hexdigest() == meta["sha256"]):
        return False
    staged = stage_zip(response, zip_path)
    if staged is None:
        return None
    adopt_zip(response, staged, zip_path)
    return True
