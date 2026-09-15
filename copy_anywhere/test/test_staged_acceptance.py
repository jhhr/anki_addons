"""The acceptance scenario from the design note, as one definition.

Everything format 2 adds appears here at once and in one run: the trigger note is edited,
a variable feeds a query, the query's notes are looped over, values are accumulated in a
list, a branch picks which note to write, the list is reduced to one value, a second loop
calls another definition once per note, that definition reads a media file, appends to it
and writes it back, its export is collected by the caller, and finally the trigger's own
cards are queried and moved.

If this passes, the pieces compose. The unit tests say each piece is right on its own; this
one says none of them quietly depends on being the only thing in the definition.
"""

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note
from copy_anywhere.logic.flow_analysis import analyze_definition, make_lookup


@pytest.fixture
def trigger(col):
    return real_anki.add_note(
        col, VOCAB, {"Word": "neko", "Reading": "ne-ko", "Meaning": "cat", "Freq": "5"}
    )


@pytest.fixture
def found_notes(col):
    """Two notes the query finds, one of which the branch sends down the other path."""
    return [
        real_anki.add_note(col, VOCAB, {"Word": "n1", "Meaning": "keep", "Freq": "1"}),
        real_anki.add_note(col, VOCAB, {"Word": "n2", "Meaning": "skip", "Freq": "2"}),
    ]


def child_definition():
    """Definition Y: read a file, append H1 to it, write it back, and export H1."""
    producer = d.variable("H1", d.text("<{{trigger.Word}}>"))
    return d.staged(
        "Y",
        guid="definition-y",
        stages=[
            producer,
            d.read_file("current", "acceptance.txt"),
            d.write_file("acceptance.txt", d.text("{{current}}{{H1}}\n")),
        ],
        exports=[d.export("H1", producer)],
    )


def parent_definition():
    return d.staged(
        "X",
        guid="definition-x",
        stages=[
            # Edit trigger A, then create M from what it now says.
            d.edit_note("trigger", [d.write("Note", d.text("start"))]),
            d.variable("M", d.text("Freq:_* {{trigger.Note}}")),
            # Build and run query A1 from a value derived from the trigger.
            d.note_query(
                "A1",
                '"Word:n1" OR "Word:n2"',
                selection={
                    "strategy": "all",
                    "count": None,
                    "sort_field": "Freq",
                    "sort_order": "ascending",
                    "sort_numeric": True,
                },
            ),
            d.list_variable("L1"),
            d.for_each_note(
                "A1",
                [
                    # Append each found note's value to L1 ...
                    d.store("L1", d.text("{{index}}:{{note.Word}}")),
                    # ... and conditionally edit either the found note or the trigger.
                    d.condition(
                        d.code("return note['Meaning'] == 'keep'"),
                        [d.edit_note("note", [d.write("Note", d.text("kept {{index}}"))])],
                        [
                            d.edit_note(
                                "trigger",
                                [d.write("Reading", d.text("skipped {{note.Word}}"))],
                            )
                        ],
                    ),
                ],
            ),
            # Reduce L1 to L1b, then create P from it.
            d.join("L1", "L1b", "|"),
            d.variable("P", d.text("P={{L1b}}")),
            d.edit_note("trigger", [d.write("Meaning", d.text("{{P}}"))]),
            # Invoke definition Y for each found note, keeping each export in a list.
            d.list_variable("H1s"),
            d.for_each_note(
                "A1",
                [
                    d.call_definition(
                        "definition-y",
                        trigger="note",
                        outputs=[{"export": "H1", "result": "H1_from_Y"}],
                    ),
                    d.store("H1s", d.text("{{H1_from_Y}}")),
                ],
            ),
            d.join("H1s", "H1_all", ","),
            d.edit_note("trigger", [d.write("Freq", d.text("{{H1_all}}"))]),
            # Finally, query the trigger's own cards and move each one.
            d.card_query("A_cards", "nid:{{trigger.__Note_ID}}"),
            d.for_each_card(
                "A_cards",
                [
                    d.edit_card(
                        "card",
                        [
                            {
                                "guid": "move",
                                "card_type_name": "",
                                "change_deck": "JP vocab",
                                "set_flag": None,
                                "suspend": None,
                                "bury": None,
                                "set_desired_retention": None,
                                "action_code": None,
                                "use_code": False,
                            }
                        ],
                    )
                ],
            ),
        ],
    )


class TestAcceptanceScenario:
    def test_the_whole_definition_analyses_cleanly(self):
        parent, child = parent_definition(), child_definition()
        result = analyze_definition(parent, lookup=make_lookup([parent, child]))
        assert result.problems == [], result.problem_messages()
        # It reaches other notes and cards, so it is not something the add hook may run.
        assert result.effects["edits_trigger"] is True
        assert result.effects["edits_other_notes"] is True
        assert result.effects["edits_cards"] is True
        assert result.effects["calls_definitions"] is True
        assert result.effects["reads_files"] is True
        assert result.effects["writes_files"] is True
        assert result.effects["add_note_compatible"] is False

    def test_it_runs_end_to_end(self, col, trigger, found_notes, media_dir, logger):
        parent, child = parent_definition(), child_definition()
        keep, skip = found_notes
        copied: list = []
        cards: dict = {}

        ok = copy_for_single_trigger_note(
            parent,
            trigger,
            copied_into_notes=copied,
            copied_into_cards_dict=cards,
            logger=logger,
            definitions_for_calls=[parent, child],
        )
        assert ok is True, logger.errors

        # The trigger: written before the loop, by the loop's else branch, and after it.
        assert trigger["Note"] == "start"
        assert trigger["Reading"] == "skipped n2"
        assert trigger["Meaning"] == "P=1:n1|2:n2"
        # One export per found note, collected in the caller's own list.
        assert trigger["Freq"] == "<n1>,<n2>"

        # The then branch wrote the found note the predicate kept, and only that one. The
        # loop walks the notes the query loaded, not the objects this test is holding, so
        # the written ones are the ones handed back for saving.
        written = {note.id: note for note in copied}
        assert written[keep.id]["Note"] == "kept 1"
        assert skip.id not in written

        # Definition Y read the file, appended, and wrote it back, once per call, so the
        # second call saw the first one's line.
        assert (media_dir / "_acceptance.txt").read_bytes() == b"<n1>\n<n2>\n"

        # Both of the trigger's cards moved. The other notes' cards are in the dict too --
        # every note a definition edits hands its cards over, because the sync path stamps
        # its flag onto all of them -- but none of them moved.
        target_deck = col.decks.id_for_name("JP vocab")
        trigger_cards = [card for card in cards.values() if card.nid == trigger.id]
        assert len(trigger_cards) == 2
        assert {card.did for card in trigger_cards} == {target_deck}
        assert all(
            card.did != target_deck for card in cards.values() if card.nid != trigger.id
        )

        # Nothing reached the database: the caller is the one that saves.
        assert col.get_note(trigger.id)["Note"] == ""
        assert {note.id for note in copied} == {trigger.id, keep.id}

    def test_a_failure_late_in_the_run_commits_none_of_it(
        self, col, trigger, found_notes, media_dir, logger
    ):
        parent, child = parent_definition(), child_definition()
        parent["stages"].append(
            d.edit_note("trigger", [d.write("Nonexistent", d.text("boom"))])
        )
        copied: list = []

        ok = copy_for_single_trigger_note(
            parent,
            trigger,
            copied_into_notes=copied,
            logger=logger,
            definitions_for_calls=[parent, child],
        )
        assert ok is False
        assert logger.has_error("not found in note")
        # Neither the notes nor the file the run had already produced are published.
        assert copied == []
        assert not (media_dir / "_acceptance.txt").exists()
