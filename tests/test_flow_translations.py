"""What the flow says, in every language the integration ships.

Home Assistant resolves a [%key:common::...%] reference only when it
builds its own integrations: in a custom one's translations the reference
reached the user as it was written. strings.json may hold references,
the translations must hold the words. Every translation carries every
key strings.json has, so no screen falls back to a raw key.

So the texts Home Assistant wants twice (a screen in both flows, a field
every key screen asks for) are copies: the copies of a screen stay equal,
and one English text is translated one way in each language.
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


def _texts(tree, prefix=""):
    """{dotted key: text} of every string of a strings file."""
    if isinstance(tree, dict):
        return {k: v for key, value in tree.items() for k, v in _texts(value, f"{prefix}{key}.").items()}
    return {prefix.rstrip("."): tree}


# English says one word where a language says two things, on purpose: the
# coach replacing a train at a station (a bus, in German) is not the coach
# a line runs (a Fernbus)
TOLD_APART = [{"common.mode_coach", "common.line_mode_coach"}]


@pytest.mark.parametrize("language", [lang for lang in LANGUAGES if lang != "en"])
def test_one_english_text_is_translated_one_way(language):
    """A screen shown twice (the source's settings, from the menu and
    from the source's own options), and a field every key screen asks
    for, carry one text: the copies Home Assistant needs, each section
    its own, must not drift apart in a translation. They did: "This
    requires an API key" read two ways in German, Spanish and Portuguese,
    the timetable's screens translated apart from the others."""
    english, translated = _texts(_read("strings.json")), _texts(_read(f"translations/{language}.json"))
    keys_of: dict[str, set[str]] = {}
    for key, text in english.items():
        keys_of.setdefault(text, set()).add(key)
    drifting = {text: sorted({translated[k] for k in keys})
                for text, keys in keys_of.items()
                if len(keys) > 1 and not any(keys <= apart for apart in TOLD_APART)
                and len({translated[k] for k in keys}) > 1}
    assert drifting == {}


# the screens of a source's settings both flows show, from the menu and
# from the source's own options: Home Assistant reads each flow's texts in
# its own section, so they are written twice and must say the same
SHARED_SCREENS = ("real_time", "real_time_key", "static_refresh", "static_refresh_key")


@pytest.mark.parametrize("name", ["strings.json"] + [f"translations/{lang}.json" for lang in LANGUAGES])
def test_a_screen_both_flows_show_says_the_same_in_both(name):
    strings = _read(name)
    for step in SHARED_SCREENS:
        assert strings["config"]["step"][step] == strings["options"]["step"][step], step


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
