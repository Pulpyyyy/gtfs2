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
