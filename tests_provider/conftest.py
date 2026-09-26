"""Bootstrap for the provider tree, and the two results files.

The Home Assistant stand-ins of tests/ha_stub.py are registered before
any test module is imported. tests/ is put on sys.path for that one
import, so the two trees share a single stub and a single answer to "what
does the integration read off Home Assistant". Nothing else of tests/ is
read from here.

At the end of a session the records each case handed over through
record_property are written to test-results/tests_provider/, beside the
results.txt files of tests/ under test-results/tests/:

    results.txt   one entry per case with its outcome, and for a case that
                  did not pass the lines that broke the promise; to read
    results.json  every check of every case, passed or not, as fields with
                  the keys sorted; to diff between two checkouts or load
                  into a tool. Written by a plain full run only, so a -k
                  or --runxfail run does not overwrite a full one

test-results/ is ignored by git, so a run leaves the tree clean. The two
files kept next to this one are a past run, as examples of the output;
a run does not rewrite them.

While a session runs, progress.txt beside them says how many tests are
done out of how many, the time spent and a guess at the time left, for a
long run followed from another window (Get-Content -Wait, tail -f). Under
pytest-xdist the workers hand their reports to the main process, which
alone counts and writes; a worker writes nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import ha_stub  # noqa: E402

ha_stub.install()


import json  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE.parent / "test-results" / "tests_provider"
_reports = []
_progress = {"total": 0, "done": 0, "start": time.monotonic()}
_worker_process = False


def _worker(config):
    return hasattr(config, "workerinput")


def pytest_configure(config):
    global _worker_process
    _worker_process = _worker(config)


def pytest_collection_finish(session):
    if not _worker(session.config):
        _progress["total"] = len(session.items)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):  # noqa: ARG001
    # every worker collects the whole session; the main process collects
    # nothing and learns the count from them
    _progress["total"] = len(ids)


def _count(report):
    if report.when == "call" or (report.when == "setup" and not report.passed):
        _progress["done"] += 1
        done, total = _progress["done"], _progress["total"]
        spent = time.monotonic() - _progress["start"]
        left = spent / done * (total - done) if total > done else 0
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "progress.txt").write_text(
            f"{done}/{total} tests, {spent / 60:.1f} min spent, "
            f"about {left / 60:.1f} min left\n", encoding="utf-8")


def pytest_runtest_logreport(report):
    if not _worker_process:
        _count(report)
    # every test of this tree, a new file included without naming it here;
    # a tests/ test run in the same session stays out
    if not report.nodeid.replace("\\", "/").startswith("tests_provider/"):
        return
    if report.when == "call" or (report.when == "setup" and report.failed):
        _reports.append(report)


def _outcome(report):
    if report.when == "setup":
        return "error"
    if hasattr(report, "wasxfail"):
        return "xfailed" if report.skipped else "xpassed"
    if report.failed and str(report.longrepr).startswith("[XPASS(strict)]"):
        return "xpassed"
    return report.outcome


def _reason(report):
    if hasattr(report, "wasxfail"):
        return report.wasxfail
    text = str(report.longrepr)
    if text.startswith("[XPASS(strict)]"):
        return text[len("[XPASS(strict)]"):].strip()
    return None


def _cases():
    cases = []
    for report in _reports:
        props = dict(report.user_properties)
        case = {"id": report.nodeid.split("[", 1)[-1].rstrip("]"),
                "outcome": _outcome(report), "reason": _reason(report),
                "checks": props.get("checks", [])}
        case.update(props.get("case", {}))
        if not case["checks"] and report.failed:
            case["error"] = [line[2:].strip() for line in
                             str(report.longrepr).splitlines()
                             if line.startswith("E ")] or [str(report.longrepr)]
        cases.append(case)
    return sorted(cases, key=lambda c: c["id"])


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if not _reports or _worker(session.config):
        return
    cases = _cases()
    lines = [f"provider case results -- {len(cases)} case(s)", ""]
    for case in cases:
        lines += [f"case: {case['id']}", f"  result: {case['outcome'].upper()}"]
        if case["reason"]:
            lines.append(f"  reason: {case['reason']}")
        broke = [c["text"] for c in case["checks"] if not c["ok"]]
        broke += case.get("error", [])
        if broke:
            lines.append("  detail:")
            lines += [f"    {text}" for text in broke]
        lines.append("")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "results.txt").write_text("\n".join(lines), encoding="utf-8")

    option = session.config.option
    partial = option.keyword or option.markexpr or option.runxfail
    if partial or exitstatus == 2:  # a -k, -m or --runxfail run, or interrupted
        return
    (RESULTS_DIR / "results.json").write_text(
        json.dumps({"cases": cases}, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n", encoding="utf-8")
