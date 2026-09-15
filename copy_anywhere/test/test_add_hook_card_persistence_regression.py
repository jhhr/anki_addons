"""Regression for deferred add-hook card-only staged mutations."""

from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.hooks.note_hooks import run_copy_fields_on_add

ADDON_TAG = "copy_anywhere"


def set_definitions(*definitions):
    mw.addonManager.configs[ADDON_TAG]["copy_definitions"] = list(definitions)


def new_note(col, note_type=VOCAB, **fields):
    model = col.models.by_name(note_type)
    assert model is not None
    note = col.new_note(model)
    for field_name, value in fields.items():
        note[field_name] = value
    return note


def deck(col, name="Other"):
    return col.decks.id_for_name(name)


def card_named(note, template_name):
    return next(card for card in note.cards() if card.template()["name"] == template_name)


def test_deferred_add_hook_persists_card_only_staged_mutations_and_undoes_them(col):
    other = real_anki.add_note(col, VOCAB, {"Word": "neko"}, deck_name="Other")
    recognition = card_named(other, "Recognition")
    definition = d.staged(
        definition_name="cards-only",
        on_add=True,
        stages=[
            d.note_query("targets", "Word:neko"),
            d.for_each_note(
                "targets",
                [
                    d.edit_note(
                        "note",
                        card_actions=[d.card_action(VOCAB, "Recognition", set_flag=3)],
                    )
                ],
            ),
        ],
    )
    set_definitions(definition)

    run_copy_fields_on_add(new_note(col, Word="neko"), deck(col))

    assert col.get_card(recognition.id).user_flag() == 3
    col.undo()
    assert col.get_card(recognition.id).user_flag() == 0
