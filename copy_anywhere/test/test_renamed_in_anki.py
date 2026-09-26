"""One pin for a rename in Anki that a stored definition never hears about.

Renaming a field breaks a definition loudly -- the reference resolves to nothing, the stage
fails and the editor refuses the save. Renaming a *card type* does not: a note-level card
action names its card type as `NoteType<::>CardType` and
`copy_primitives.card_actions_by_template_name` matches that string against the live
template name, so after a rename the action matches no card and the run goes on as though
the definition never had one. Nothing is logged at any level the user sees.

The pin is xfail rather than a characterization test because it is an open item, not a wart
worth keeping: see "Following a rename in Anki" in `docs/follow-ups.md`. It turns green only
when *this run* says so and still hands over no card, which none of that section's tiers
does as written: (a) reports stale names when the collection loads, not during a run, and
(b) and (c) make the action follow the rename, so a card is handed over. Whichever is built
has to rewrite this pin to ask what that tier promises; strict, so it cannot be forgotten.
"""

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note


@pytest.mark.xfail(reason="a run does not report a card type it cannot find", strict=True)
def test_a_card_action_whose_card_type_was_renamed_says_so(col, logger):
    note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
    definition = d.within_note(
        field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
        card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)],
    )

    # The user renames the card type in Anki's card layout screen, long after writing the
    # definition. No hook carries a template rename, so nothing tells the addon.
    note_type = col.models.by_name(VOCAB)
    note_type["tmpls"][0]["name"] = "Reading card"
    col.models.update_dict(note_type)

    copied_into_cards: dict = {}
    ok = copy_for_single_trigger_note(
        definition, note, copied_into_cards_dict=copied_into_cards
    )

    # The field write still lands, so the run looks like a success from the outside.
    assert ok is True
    assert note["Note"] == "neko"
    # The flag the definition asks for is set on neither card -- no card is handed over to
    # be saved at all -- and nothing says why.
    assert copied_into_cards == {}
    assert logger.errors or logger.warnings
