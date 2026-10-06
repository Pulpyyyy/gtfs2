"""The key filter sits on the logger of every module, those of a subpackage
too.

A filter only sees the lines of the logger it sits on, and every module
logs under its own name: a module the filter was not put on would write
a key in the clear.
"""
from __future__ import annotations

import logging

import ha_stub

key_mask = ha_stub.load("key_mask")


def test_every_module_of_the_folders_is_named(tmp_path):
    (tmp_path / "top.py").write_text("")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "inner.py").write_text("")
    names = set(key_mask._module_names("comp", [str(tmp_path)]))
    assert names == {"comp.top", "comp.pkg", "comp.pkg.inner"}


def test_a_line_from_a_subpackage_module_is_masked(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "inner.py").write_text("")
    key_mask.note_key("d00dfeed-0000-4000-8000-00000000000b")
    key_mask.hide_keys_in_logs("comp_under_test", [str(tmp_path)])
    record = logging.LogRecord("comp_under_test.pkg.inner", logging.WARNING, __file__, 1,
                               "fetching %s", ("https://h/d00dfeed-0000-4000-8000-00000000000b.zip",), None)
    for log_filter in logging.getLogger("comp_under_test.pkg.inner").filters:
        log_filter.filter(record)
    assert "d00dfeed" not in record.getMessage()
