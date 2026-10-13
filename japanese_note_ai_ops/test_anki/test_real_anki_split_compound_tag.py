"""The `vocab::split-compound-word` sweep in a running Anki.

What the stand-in collection of test/test_split_compound_tag.py cannot say: that Anki's search
finds the tagged notes and the arrays that have a word whole, that the tags are really set, in
one undo step, by a `CollectionOp` started from one of Anki's timers, and that the list reaches
the config the real `AddonManager` reads off disk, at a moment when nothing copies an older
config back over it.

The handlers are called directly rather than through `gui_hooks`: by the time this file runs,
`media_sync_did_start_or_stop` still holds the handlers of main windows that earlier tests in
the process have closed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest
from aqt import gui_hooks

from anki_shared.testing import real_anki
from anki_shared.word_array.field_text import decode_word_array, format_word_array
from japanese_note_ai_ops import split_compound_tag as tag

from .conftest import BASE_CONFIG, FIELDS, NOTETYPE, PACKAGE

WAIT = 30000
WORD = FIELDS["word_kanjified_field"]
ARRAY = FIELDS["word_list_field"]
SORT = FIELDS["word_sort_field"]
LIST_KEY = "split_compound_words"


def compound_element(
    match_data: Optional[list] = None, form: str = "毛羽立つ", split: bool = False
) -> list:
    """毛羽立ち as an array holds it: whole, or with its two words under it."""
    subs = [
        [" 毛羽[けば]", "noun", "毛羽", "けば", [], []],
        ["立[た]ち", "verb", "立つ", "たつ", [], []],
    ]
    return [
        " 毛羽[けば]立[た]ち",
        "verb",
        form,
        "けばだつ",
        match_data or [],
        subs if split else [],
    ]


def add(mw: Any, word: str, array: Optional[list] = None, tags: Optional[list[str]] = None) -> Any:
    fields = {WORD: word, SORT: word}
    if array is not None:
        fields[ARRAY] = format_word_array(array)
    return real_anki.add_note(mw.col, NOTETYPE, fields, tags=tags)


def listed(mw: Any) -> Any:
    return (mw.addonManager.getConfig(PACKAGE) or {}).get(LIST_KEY)


def to_regenerate(mw: Any) -> set:
    """By the search a person would type into the browser."""
    return set(mw.col.find_notes(f'"tag:{tag.REGENERATE_TAG}"'))


@pytest.fixture
def swept(monkeypatch):
    """How many sweeps have run and what they told the user; no log file is opened."""
    seen = SimpleNamespace(count=0, tooltips=[])
    sweep = tag.sweep

    def counted() -> None:
        sweep()
        seen.count += 1

    monkeypatch.setattr(tag, "sweep", counted)
    monkeypatch.setattr(tag, "tooltip", lambda text, **kwargs: seen.tooltips.append(text))
    monkeypatch.setattr(tag, "start_call_log", lambda name: None)
    # A test is one session of Anki's: what an earlier one listed is not remembered
    monkeypatch.setattr(tag, "_listed_this_session", {})
    return seen


def settle(anki_session, mw: Any, swept: Any, sweeps: int = 1) -> None:
    """Until the sweep's timer has fired and the operation it started is over."""
    anki_session.qtbot.waitUntil(
        lambda: swept.count >= sweeps and mw._background_op_count == 0, timeout=WAIT
    )


def test_a_tagged_notes_word_is_listed_and_the_notes_that_have_it_whole_are_tagged(
    anki_session, real_mw, config, swept
):
    config()
    compound = add(real_mw, "毛羽立つ", tags=[tag.SPLIT_COMPOUND_TAG])
    surface = ["表面", "noun", "表面", "ひょうめん", [], []]
    linked = add(real_mw, "毛羽", [surface, compound_element([compound.id, 4])])
    not_linked = add(real_mw, "布", [compound_element(["match"])])
    as_a_sub_word = add(
        real_mw,
        "糸",
        [[
            "毛羽立ちと",
            "expression",
            "毛羽立ちと",
            "けばだちと",
            [],
            [compound_element(), ["と"]],
        ]],
    )
    split_already = add(real_mw, "紙", [compound_element([compound.id, 4], split=True)])
    another_word = add(real_mw, "目", [["目立[めだ]った", "verb", "目立つ", "めだつ", [], []]])

    tag.on_sync_did_finish()
    settle(anki_session, real_mw, swept)

    assert listed(real_mw) == ["毛羽立つ"]
    # Every array that has the word whole, linked to the tagged note or not: the list is by
    # word, so the next regeneration splits it in each of them
    assert to_regenerate(real_mw) == {linked.id, not_linked.id, as_a_sub_word.id}
    assert not {split_already.id, another_word.id} & to_regenerate(real_mw)
    assert swept.tooltips == [
        "Compounds to split: 毛羽立つ listed. 3 notes tagged sentence::regenerate."
    ]
    # The tag stays: it is what puts the word back should the list lose it
    assert real_mw.col.get_note(compound.id).has_tag(tag.SPLIT_COMPOUND_TAG)
    # Nothing but the tag changed on the notes
    array = decode_word_array(real_mw.col.get_note(linked.id)[ARRAY])
    assert array is not None and array[1] == compound_element([compound.id, 4])

    # One undo step takes the tags back off
    assert real_mw.col.undo_status().undo == tag.UNDO_LABEL
    real_mw.col.undo()
    assert to_regenerate(real_mw) == set()


def test_a_word_the_list_has_is_passed_over(anki_session, real_mw, config, swept):
    config(**{LIST_KEY: ["毛羽立つ"]})
    compound = add(real_mw, "毛羽立つ", tags=[tag.SPLIT_COMPOUND_TAG])
    add(real_mw, "毛羽", [compound_element([compound.id, 4])])
    undo_before = real_mw.col.undo_status().undo

    tag.on_sync_did_finish()
    settle(anki_session, real_mw, swept)

    # The notes are not tagged again at every sync: someone who regenerated one and found the
    # compound has no two words to split into took the tag off for good
    assert to_regenerate(real_mw) == set()
    assert listed(real_mw) == ["毛羽立つ"]
    assert swept.tooltips == []
    assert real_mw.col.undo_status().undo == undo_before


def test_a_word_that_fell_off_the_list_goes_back_on_after_the_media_sync_that_took_it(
    anki_session, real_mw, config, swept
):
    config()
    compound = add(real_mw, "毛羽立つ", tags=[tag.SPLIT_COMPOUND_TAG])
    regenerated = add(real_mw, "毛羽", [compound_element([compound.id, 4])])
    left_whole = add(real_mw, "布", [compound_element([compound.id, 4])])
    tag.on_sync_did_finish()
    settle(anki_session, real_mw, swept)
    assert listed(real_mw) == ["毛羽立つ"]
    assert to_regenerate(real_mw) == {regenerated.id, left_whole.id}

    # Both notes are regenerated and their tags taken off; in one the compound split, in the
    # other it stayed whole, as one the generator finds no two words in does. Then a media
    # sync that no collection sync started ends, and the config is replaced by one from before
    # the word was listed, as Addon Config Sync does with the copy in the media folder
    now_split = real_mw.col.get_note(regenerated.id)
    now_split[ARRAY] = format_word_array([compound_element([compound.id, 4], split=True)])
    for note in (now_split, real_mw.col.get_note(left_whole.id)):
        note.remove_tag(tag.REGENERATE_TAG)
        real_mw.col.update_note(note)
    real_mw.addonManager.writeConfig(PACKAGE, dict(BASE_CONFIG))
    assert listed(real_mw) is None and to_regenerate(real_mw) == set()

    tag.on_media_sync_did_start_or_stop(False)
    settle(anki_session, real_mw, swept, sweeps=2)

    # Back on the list, with nothing said and no note tagged a second time
    assert listed(real_mw) == ["毛羽立つ"]
    assert to_regenerate(real_mw) == set()
    assert len(swept.tooltips) == 1

    # After a restart of Anki the same is a listing like the first: the note that still has
    # the word whole is tagged again
    tag._listed_this_session.clear()
    real_mw.addonManager.writeConfig(PACKAGE, dict(BASE_CONFIG))
    tag.on_media_sync_did_start_or_stop(False)
    settle(anki_session, real_mw, swept, sweeps=3)

    assert listed(real_mw) == ["毛羽立つ"]
    assert to_regenerate(real_mw) == {left_whole.id}
    assert len(swept.tooltips) == 2


def test_the_form_an_array_shows_the_word_by_is_listed_with_the_notes_own(
    anki_session, real_mw, config, swept
):
    # The note spells its word 毛羽だつ, the arrays linked to it show 毛羽立つ, and that is the
    # form the generator looks a compound up by
    config()
    compound = add(real_mw, "毛羽だつ", tags=[tag.SPLIT_COMPOUND_TAG])
    sentence = add(real_mw, "毛羽", [compound_element([compound.id])])
    add(real_mw, "布", [compound_element([compound.id + 1, 4], form="別の語")])

    tag.on_sync_did_finish()
    settle(anki_session, real_mw, swept)

    assert listed(real_mw) == ["毛羽だつ", "毛羽立つ"]
    assert to_regenerate(real_mw) == {sentence.id}


def test_a_child_of_the_tag_or_a_note_type_with_no_word_field_lists_nothing(
    anki_session, real_mw, config, swept
):
    config()
    child = add(real_mw, "毛羽", [compound_element()], tags=[f"{tag.SPLIT_COMPOUND_TAG}::new"])
    basic = real_anki.add_note(
        real_mw.col, "Basic", {"Front": "毛羽立つ"}, tags=[tag.SPLIT_COMPOUND_TAG]
    )
    # Anki's search for a tag finds its children as well, so the search alone would list 毛羽
    found = set(real_mw.col.find_notes(f'"tag:{tag.SPLIT_COMPOUND_TAG}"'))
    assert found == {child.id, basic.id}

    tag.on_sync_did_finish()
    settle(anki_session, real_mw, swept)

    assert listed(real_mw) is None
    assert to_regenerate(real_mw) == set()
    assert swept.tooltips == []


def test_the_list_is_written_after_the_media_sync_and_after_that_calls_other_handlers(
    anki_session, real_mw, config, swept, monkeypatch
):
    config()
    compound = add(real_mw, "毛羽立つ", tags=[tag.SPLIT_COMPOUND_TAG])
    sentence = add(real_mw, "毛羽", [compound_element([compound.id, 4])])
    monkeypatch.setattr(real_mw.media_syncer, "_syncing", True)
    # What Addon Config Sync copied out to the media folder when the sync started
    saved_at_sync_start = dict(real_mw.addonManager.addonMeta(PACKAGE))

    tag.on_sync_did_finish()
    anki_session.qtbot.wait(tag.SWEEP_DELAY_MS * 4)

    assert swept.count == 0 and listed(real_mw) is None and to_regenerate(real_mw) == set()

    # The media sync stops. Addon Config Sync is another handler of that hook call, here
    # running after ours, and copies the config back from the media folder
    monkeypatch.setattr(real_mw.media_syncer, "_syncing", False)
    tag.on_media_sync_did_start_or_stop(False)
    real_mw.addonManager.writeAddonMeta(PACKAGE, saved_at_sync_start)
    settle(anki_session, real_mw, swept)

    assert listed(real_mw) == ["毛羽立つ"]
    assert to_regenerate(real_mw) == {sentence.id}


def test_only_the_list_is_added_to_the_users_own_config(anki_session, real_mw, swept):
    # config.json's defaults stay defaults: written into meta.json they would no longer follow
    # a later version's
    defaults = dict(BASE_CONFIG, proper_nouns_model="the shipped default", **{LIST_KEY: []})
    users = {NOTETYPE: BASE_CONFIG[NOTETYPE], "log_to_console": False}
    with anki_session.addon_config_created(
        package_name=PACKAGE, default_config=defaults, user_config=dict(users)
    ):
        add(real_mw, "毛羽立つ", tags=[tag.SPLIT_COMPOUND_TAG])

        tag.on_sync_did_finish()
        settle(anki_session, real_mw, swept)

        assert real_mw.addonManager.addonMeta(PACKAGE)["config"] == dict(
            users, **{LIST_KEY: ["毛羽立つ"]}
        )
        merged = real_mw.addonManager.getConfig(PACKAGE)
        assert merged["proper_nouns_model"] == "the shipped default"
        # No array has the word, so no note is tagged and there is nothing to undo
        assert swept.tooltips == ["Compounds to split: 毛羽立つ listed."]
        assert real_mw.col.undo_status().undo != tag.UNDO_LABEL


def test_init_registers_the_sweep_on_both_sync_hooks():
    # Needs no Anki; the conftest's autouse fixture takes the two handlers off again
    finished, media = gui_hooks.sync_did_finish, gui_hooks.media_sync_did_start_or_stop
    before = (finished.count(), media.count())

    tag.init_split_compound_tag()

    assert (finished.count(), media.count()) == (before[0] + 1, before[1] + 1)
    assert finished._hooks[-1] is tag.on_sync_did_finish
    assert media._hooks[-1] is tag.on_media_sync_did_start_or_stop
