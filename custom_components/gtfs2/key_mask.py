"""Keep the sources' api keys out of the logs and off the screens.

A key is typed once and then travels far: in the url when it rides in the
query string, in the headers, in the dicts the debug lines print, and in
the exceptions requests raises, which quote the full url. Guarding each of
the hundred-odd log lines one by one would miss the next one written, so a
single filter sits on the integration's loggers and writes KEY_MASK
wherever a known key shows up. The flow's key screens show the same mask
for a stored key, so the key itself never goes back to the browser. And a
key sent in a header goes to the host it was given for, not to the one a
redirect points at (see fetch).
"""
import base64
from collections.abc import Iterable, Mapping
import logging
import pkgutil
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import requests

from .const import CONF_API_KEY
from .file_url import FILE_SCHEME, FileAdapter

if TYPE_CHECKING:
    # for the annotations only
    from homeassistant.config_entries import ConfigEntry

KEY_MASK = "*****"

# shorter values are not keys, and masking them would garble the logs
_KEY_MIN_LENGTH = 4

# every key the integration knows: the ones stored on the entries, noted at
# their setup, and the ones typed in the flow or a service call, noted on
# arrival. Replaced whole rather than grown in place: log lines come from
# executor threads, and they must never see the set change under them.
_known_keys: frozenset[str] = frozenset()


def note_key(key: object) -> None:
    """Remember a key, so that from now on it is masked wherever it shows."""
    global _known_keys
    if not isinstance(key, str) or len(key.strip()) < _KEY_MIN_LENGTH:
        return
    key = key.strip()
    # in a url the key may travel percent-encoded, and in an HTTP Basic
    # header base64-encoded
    _known_keys = _known_keys | {key, quote(key, safe=""), _basic_login(key)}


def _basic_login(key: str) -> str:
    """The base64 login of an HTTP Basic header for a key: the key as the
    user with no password, or the user:password it holds itself."""
    login = key if ":" in key else key + ":"
    return base64.b64encode(login.encode("utf-8")).decode("ascii")


def basic_credentials(key: str) -> str:
    """The Authorization header value that sends a key as an HTTP Basic
    login (the CTS of Strasbourg takes its key so)."""
    return "Basic " + _basic_login(key)


def note_entry_keys(entry: ConfigEntry) -> None:
    """Remember the keys an entry carries: the static one, the realtime one."""
    note_key(entry.data.get(CONF_API_KEY))
    note_key(entry.options.get(CONF_API_KEY))


def hide_keys(text: object) -> str:
    """The text with every known key replaced by the mask."""
    text = str(text)
    for key in _known_keys:
        if key in text:
            text = text.replace(key, KEY_MASK)
    return text


class _HideKeys(logging.Filter):
    """Mask the known keys in a log line and in the exception it carries."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _known_keys:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pylint: disable=broad-except
            # a malformed line is logging's own error to report
            return True
        hidden = hide_keys(message)
        if record.exc_info:
            trace = logging.Formatter().formatException(record.exc_info)
            hidden_trace = hide_keys(trace)
            if hidden_trace != trace:
                # the traceback quotes the key (a requests error names the
                # url): it rides in the message, masked, instead of being
                # formatted again from the exception by each handler
                hidden = hidden + "\n" + hidden_trace
                record.exc_info = None
                record.exc_text = None
        if hidden != message:
            record.msg, record.args = hidden, None
        return True


# the headers the integration sends that carry nothing of the user's
_SAFE_ON_REDIRECT = frozenset({"user-agent", "accept"})


class _KeyStaysHome(requests.Session):
    """A session that sends the caller's headers to their own host only.

    requests drops Authorization when a redirect leads to another host,
    and nothing else: a key in a header of its own (x-api-key, apikey...)
    followed the redirect to the CDN or the bucket serving the file. The
    same rule now covers every header the caller gave, the harmless ones
    apart.
    """

    def __init__(self, headers: Mapping[str, str | None] | None) -> None:
        super().__init__()
        self._given = {name.lower() for name in (headers or {})} - _SAFE_ON_REDIRECT
        # a feed on this machine is fetched by its file:// url like any other
        self.mount(FILE_SCHEME, FileAdapter())

    def rebuild_auth(self, prepared_request: requests.PreparedRequest,
                     response: requests.Response) -> None:
        super().rebuild_auth(prepared_request, response)
        if self.should_strip_auth(response.request.url, prepared_request.url):
            for name in list(prepared_request.headers):
                if name.lower() in self._given:
                    del prepared_request.headers[name]


def fetch(method: str, url: str, headers: Mapping[str, str | None] | None = None, **kwargs: Any) -> requests.Response:
    """requests.request, with the caller's headers kept from other hosts,
    and a file:// url answered as a host would (file_url)."""
    with _KeyStaysHome(headers) as session:
        return session.request(method, url, headers=headers, **kwargs)


def hide_keys_in_logs(package: str, path: Iterable[str]) -> None:
    """Put the filter on the package's logger and on each of its modules'.

    A filter only sees the lines of the logger it sits on, not those its
    children pass up, and every module logs under its own name.
    """
    key_filter = _HideKeys()
    names = [package] + [f"{package}.{module.name}" for module in pkgutil.iter_modules(path)]
    for name in names:
        logging.getLogger(name).addFilter(key_filter)
