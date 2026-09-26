"""What the flow says, in every language the integration ships.

Home Assistant resolves a [%key:common::...%] reference only when it
builds its own integrations: in a custom one's translations the reference
reached the user as it was written. strings.json may hold references,
the translations must hold the words. Every translation carries every
key strings.json has, so no screen falls back to a raw key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "gtfs2"
LANGUAGES = sorted(p.stem for p in (COMPONENT / "translations").glob("*.json"))


def _keys(tree, prefix=""):
    if isinstance(tree, dict):
        return {k for key, value in tree.items() for k in _keys(value, f"{prefix}{key}.")}
    return {prefix.rstrip(".")}


def _leaves(tree):
    if isinstance(tree, dict):
        return [leaf for value in tree.values() for leaf in _leaves(value)]
    return [tree]


def _read(name):
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_translation_holds_words_not_references(language):
    texts = _leaves(_read(f"translations/{language}.json"))
    assert [t for t in texts if isinstance(t, str) and "[%key:" in t] == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_translation_carries_every_key(language):
    assert _keys(_read(f"translations/{language}.json")) == _keys(_read("strings.json"))


@pytest.mark.parametrize("name", ["strings.json", *(f"translations/{language}.json" for language in LANGUAGES)])
def test_a_progress_title_asks_for_no_placeholder(name):
    """The frontend fills a progress screen's description with the flow's
    placeholders, but reads its title without them: a {file} there showed
    as MISSING_VALUE on the install (2026-09-26)."""
    words = _read(name)
    found = []
    for flow in ("config", "options"):
        steps = words.get(flow, {}).get("step", {})
        for step_id in words.get(flow, {}).get("progress", {}):
            title = steps.get(step_id, {}).get("title", "")
            if "{" in title:
                found.append(f"{flow}.step.{step_id}.title: {title}")
    assert found == []
