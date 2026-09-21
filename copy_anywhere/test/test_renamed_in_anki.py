"""One pin for a rename in Anki that a stored definition never hears about.

Renaming a field breaks a definition loudly -- the reference resolves to nothing, the stage
fails and the editor refuses the save. Renaming a *card type* used to do the opposite: a
note-level card action named its card type as the string `NoteType<::>CardType`, which was
matched against the live template name, so after a rename the action matched no card and
the run went on as though the definition never had one, with nothing logged at any level
the user sees.

An action written since then carries the template's id and follows the rename
(`test_object_refs.py`). This one carries only the name -- the shape a README example and a
hand-edited definition still have -- so the action cannot land; what it must not do is
land nowhere in silence. That is what is pinned here: see "Following a rename in Anki" in
`docs/follow-ups.md`.
"""

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note


def test_a_card_action_whose_card_type_was_renamed_says_so(col, logger):
    note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
    definition = d.within_note(
        field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
        card_actions=[d.card_action(VOCAB, "Recognition", set_flag=2)],
    )

    # The user renames the card type in Anki's card layout screen, long after writing the
    # definition. No hook carries a template rename, so nothing tells the addon, and this
    # action has no template id to be followed by.
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
    # The flag the definition asks for is not set on either card -- and now something says
    # why.
    assert [card.flags for card in copied_into_cards.values()] == [0, 0]
    assert logger.errors or logger.warnings
