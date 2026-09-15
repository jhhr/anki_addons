"""Regression for staged card edits on the unfocus fast path."""

from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.hooks.note_hooks import run_copy_fields_on_unfocus_field

ADDON_TAG = "copy_anywhere"
WORD = 0


def set_definitions(*definitions):
    mw.addonManager.configs[ADDON_TAG]["copy_definitions"] = list(definitions)


def existing_note(col, note_type=VOCAB, **fields):
    return real_anki.add_note(col, note_type, fields, deck_name="Other")


def card_named(note, template_name):
    return next(card for card in note.cards() if card.template()["name"] == template_name)


def test_unfocus_persists_staged_card_edits_without_writing_note_edits(col):
    note = existing_note(col, Word="neko")
    recognition = card_named(note, "Recognition")
    definition = d.staged(
        definition_name="cards-only",
        on_unfocus={"edit_fields": ["Word"], "add_fields": []},
        stages=[
            d.card_query("cards", "nid:{{trigger.__Note_ID}}"),
            d.for_each_card(
                "cards",
                [
                    d.edit_card("card", [{
                        "guid": "flag",
                        "card_type_name": "",
                        "change_deck": None,
                        "set_flag": 4,
                        "suspend": None,
                        "bury": None,
                        "set_desired_retention": None,
                        "action_code": None,
                        "use_code": False,
                    }])
                ],
            ),
        ],
    )
    set_definitions(definition)

    run_copy_fields_on_unfocus_field(False, note, WORD)

    assert col.get_card(recognition.id).user_flag() == 4
    assert col.get_note(note.id)["Note"] == ""
