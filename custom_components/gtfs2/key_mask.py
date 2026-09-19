"""Keep the sources' api keys out of the logs and off the screens.

A key is typed once and then travels far: in the url when it rides in the
query string, in the headers, in the dicts the debug lines print, and in
the exceptions requests raises, which quote the full url. Guarding each of
the hundred-odd log lines one by one would miss the next one written, so a
single filter sits on the integration's loggers and writes KEY_MASK
wherever a known key shows up. The flow's key screens show the same mask
for a stored key, so the key itself never goes back to the browser.
"""
import logging
import pkgutil
from urllib.parse import quote

from .const import CONF_API_KEY

KEY_MASK = "*****"

# shorter values are not keys, and masking them would garble the logs
_KEY_MIN_LENGTH = 4

# every key the integration knows: the ones stored on the entries, noted at
# their setup, and the ones typed in the flow or a service call, noted on
# arrival. Replaced whole rather than grown in place: log lines come from
# executor threads, and they must never see the set change under them.
_known_keys = frozenset()


def note_key(key):
    """Remember a key, so that from now on it is masked wherever it shows."""
    global _known_keys
    if not isinstance(key, str) or len(key.strip()) < _KEY_MIN_LENGTH:
        return
    key = key.strip()
    # in a url the key may travel percent-encoded
    _known_keys = _known_keys | {key, quote(key, safe="")}


def note_entry_keys(entry):
    """Remember the keys an entry carries: the static one, the realtime one."""
    note_key(entry.data.get(CONF_API_KEY))
    note_key(entry.options.get(CONF_API_KEY))


def hide_keys(text):
    """The text with every known key replaced by the mask."""
    text = str(text)
    for key in _known_keys:
        if key in text:
            text = text.replace(key, KEY_MASK)
    return text


class _HideKeys(logging.Filter):
    """Mask the known keys in a log line and in the exception it carries."""

    def filter(self, record):
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


def hide_keys_in_logs(package, path):
    """Put the filter on the package's logger and on each of its modules'.

    A filter only sees the lines of the logger it sits on, not those its
    children pass up, and every module logs under its own name.
    """
    key_filter = _HideKeys()
    names = [package] + [f"{package}.{module.name}" for module in pkgutil.iter_modules(path)]
    for name in names:
        logging.getLogger(name).addFilter(key_filter)
