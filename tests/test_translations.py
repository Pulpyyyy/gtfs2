"""What the flow says, in every language the integration ships.

Home Assistant resolves a [%key:common::...%] reference only when it
builds its own integrations: in a custom one's translations the reference
reached the user as it was written. strings.json may hold references,
the translations must hold the words. Every translation carries every
key strings.json has, so no screen falls back to a raw key.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "gtfs2"
LANGUAGES = sorted(p.stem for p in (COMPONENT / "translations").glob("*.json"))
# a {name} the frontend replaces; {{ }} is a literal brace
PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_]\w*)\}(?!\})")


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


def _menu_steps():
    """The step_ids the flows show as menus, read from their code."""
    steps = set()
    for path in COMPONENT.glob("*.py"):
        for call in re.findall(r"async_show_menu\((.*?)\)", path.read_text(encoding="utf-8"), re.S):
            steps.update(re.findall(r'step_id="(\w+)"', call))
    return steps


@pytest.mark.parametrize("name", ["strings.json", *(f"translations/{language}.json" for language in LANGUAGES)])
def test_a_progress_or_menu_title_asks_for_no_placeholder(name):
    """The frontend fills a screen's description, its fields and its menu
    options with the flow's placeholders, but reads the title of a progress
    screen and of a menu without them: a {file} there showed as
    MISSING_VALUE on the install (2026-09-26)."""
    words = _read(name)
    menus = _menu_steps()
    assert "user" in menus
    found = []
    for flow in ("config", "options"):
        steps = words.get(flow, {}).get("step", {})
        for step_id in {*words.get(flow, {}).get("progress", {}), *menus}:
            title = steps.get(step_id, {}).get("title", "")
            if PLACEHOLDER.search(title):
                found.append(f"{flow}.step.{step_id}.title: {title}")
    assert found == []


def _texts(tree, prefix=""):
    if isinstance(tree, dict):
        return {k: v for key, value in tree.items() for k, v in _texts(value, f"{prefix}{key}.").items()}
    return {prefix.rstrip("."): tree}


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_translation_asks_for_the_placeholders_english_does(language):
    """A placeholder the flow does not pass shows as MISSING_VALUE: a
    translation may not ask for one strings.json does not, nor drop one."""
    english = _texts(_read("strings.json"))
    found = [
        key for key, text in _texts(_read(f"translations/{language}.json")).items()
        if set(PLACEHOLDER.findall(str(text))) != set(PLACEHOLDER.findall(str(english.get(key, ""))))
    ]
    assert found == []
