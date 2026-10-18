"""Fill link IDs' plan over the selected notes of every type (sync_local_ops/fill_link_ids.py):
the fields and word arrays it writes, the notes it leaves out, and its report.

The rules for one type's ID fields are test_link_ids.py's (`plan_id_fills`). The run in a real
collection, its one undo step, and the migration's pre-flight and find_missing passing after it
are test_replay/test_fill_link_ids.py's.
"""

import unittest

from addon_modules import load_ops_module

fill = load_ops_module("fill_link_ids", subdir="sync_local_ops")
# The module the op itself imports, so its LinkIndex is the op's
link_ids = load_ops_module("link_ids", subdir="")
match_flags = load_ops_module("match_flags", subdir="word_array")

VOCAB = "Vocab note"
SENTENCE = "Sentence note"
# Has the vocab type's sort field but names no link ID field: its notes are left out
OTHER = "Other note"
TWO_TYPE = {
    SENTENCE: {
        "vocab_note_type": VOCAB,
        "word_list_field": "Words",
        "sentence_field": "Text",
        "sentence_id_field": "Sentence ID",
    },
    VOCAB: {
        "sentence_note_type": SENTENCE,
        "word_sort_field": "Sort",
        "new_note_id_field": "Word ID",
    },
    OTHER: {"word_sort_field": "Sort"},
}
ONE_TYPE = {
    VOCAB: {"word_list_field": "Words", "word_sort_field": "Sort", "new_note_id_field": "Word ID"}
}

NOTE_ID = 1700000000000
GENERATED = 1160697660039337
SENTENCE_GENERATED = 4000000000000001
PLACEHOLDER = -777
UNHELD = -999


class Note:
    """A note in hand: its id and its fields."""

    def __init__(self, note_id: int, fields: dict) -> None:
        self.id = note_id
        self.fields = fields

    def __contains__(self, key: str) -> bool:
        return key in self.fields

    def __getitem__(self, key: str) -> str:
        return self.fields[key]


def word(form: str, match_data: list) -> list:
    return [form, "noun", form, form, match_data, []]


def words(*elements: list) -> str:
    return match_flags.format_word_array(list(elements))


class Collection:
    """Notes of each type, numbered in the order made, as note ids are."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.notes: dict = {}

    def add(self, note_type: str, own_id: bool = False, **fields: str) -> Note:
        """`own_id`: its link ID field holds its own note id, as a note an op saved holds."""
        note = Note(NOTE_ID + sum(len(n) for n in self.notes.values()), dict(fields))
        if own_id:
            note.fields[link_ids_field(self.config, note_type)] = str(note.id)
        self.notes.setdefault(note_type, []).append(note)
        return note

    def indexes(self) -> dict:
        """Every type's index, of all its notes, as the run reads them."""
        out = {}
        for name, notes in self.notes.items():
            field = link_ids_field(self.config, name)
            rows = [(note.id, note[field] if field else "") for note in notes]
            out[name] = link_ids.LinkIndex.from_rows(rows)
        return out

    def plan(self, selected=None):
        """The plan over every note, or over `selected`."""
        chosen = {}
        for name, notes in self.notes.items():
            picked = [note for note in notes if selected is None or note in selected]
            if picked:
                chosen[name] = picked
        return fill.plan_fill(self.config, chosen, self.indexes())

    def write(self, plan) -> None:
        by_id = {note.id: note for notes in self.notes.values() for note in notes}
        for note_id, fields in plan.writes().items():
            by_id[note_id].fields.update(fields)


def link_ids_field(config: dict, name: str):
    block = config.get(name, {})
    return block.get("sentence_id_field") or block.get("new_note_id_field")


class TwoTypeFillTests(unittest.TestCase):
    def setUp(self):
        col = self.col = Collection(TWO_TYPE)
        self.empty = col.add(VOCAB, **{"Sort": "cat", "Word ID": ""})
        self.placeholder_holder = col.add(VOCAB, **{"Sort": "eat", "Word ID": str(PLACEHOLDER)})
        self.original = col.add(VOCAB, **{"Sort": "fish", "Word ID": str(GENERATED)})
        # Anki's Create Copy of it
        self.copy = col.add(VOCAB, **{"Sort": "fish", "Word ID": str(GENERATED)})
        self.sentence = col.add(
            SENTENCE,
            **{
                "Text": "s1",
                "Sentence ID": "",
                "Words": words(
                    word("fish", [GENERATED, 4]),
                    word("eat", [PLACEHOLDER, 3]),
                    word("run", [UNHELD]),
                ),
            },
        )
        self.sentence_original = col.add(
            SENTENCE,
            **{"Text": "s2", "Sentence ID": str(SENTENCE_GENERATED), "Words": words()},
        )
        self.sentence_copy = col.add(
            SENTENCE,
            **{"Text": "s2", "Sentence ID": str(SENTENCE_GENERATED), "Words": words()},
        )
        self.other = col.add(OTHER, **{"Sort": "bird"})

    def test_every_kind_of_write_to_the_notes_of_both_types(self):
        plan = self.col.plan()

        self.assertEqual(
            plan.writes(),
            {
                self.empty.id: {"Word ID": str(self.empty.id)},
                self.placeholder_holder.id: {"Word ID": str(self.placeholder_holder.id)},
                self.copy.id: {"Word ID": str(self.copy.id)},
                self.sentence.id: {
                    "Sentence ID": str(self.sentence.id),
                    # The placeholder's holder linked by its link ID, its note id, the quality
                    # kept; the one no note holds matched again
                    "Words": words(
                        word("fish", [GENERATED, 4]),
                        word("eat", [self.placeholder_holder.id, 3]),
                        word("run", ["match"]),
                    ),
                },
                self.sentence_copy.id: {"Sentence ID": str(self.sentence_copy.id)},
            },
        )
        self.assertEqual(plan.skipped, {OTHER: 1})
        self.assertEqual(plan.selected, 8)

    def test_the_vocab_notes_holding_placeholders_need_not_be_selected(self):
        plan = self.col.plan(selected=[self.sentence])

        self.assertEqual(set(plan.writes()), {self.sentence.id})
        self.assertIn(
            f"[{self.placeholder_holder.id}, 3]", plan.writes()[self.sentence.id]["Words"]
        )

    def test_the_vocab_notes_old_arrays_are_never_read(self):
        # The vocab type keeps its old array field, under the sentence block's name, until the
        # user deletes it: in the two-type layout no op reads it
        self.empty.fields["Words"] = words(word("cat", [UNHELD]))

        plan = self.col.plan()

        self.assertNotIn("Words", plan.writes()[self.empty.id])
        self.assertEqual([p.note_id for p in plan.placeholders], [self.sentence.id] * 2)

    def test_a_second_run_writes_nothing(self):
        self.col.write(self.col.plan())

        plan = self.col.plan()

        self.assertEqual(plan.writes(), {})
        self.assertEqual(plan.placeholders, [])
        for index in self.col.indexes().values():
            self.assertEqual((index.duplicates, index.invalid), ({}, {}))
        self.assertIn("Nothing to write: no selected note was changed.", fill.report_text(plan))

    def test_the_report_counts_each_kind_and_names_each_holder(self):
        report = fill.report_text(self.col.plan())

        for line in [
            "  notes selected: 8",
            "  notes written: 5",
            "  empty ID fields given their note id: 2",
            "  ID fields holding something but an ID given their note id: 1",
            "  IDs more than one note held: 2",
            "  holders given their own note id: 2",
            "  holders given a generated ID: 0",
            "  legacy placeholders linked to the note holding them: 1",
            "  legacy placeholders no note holds, set to match: 1",
            "  legacy placeholders several notes hold, left: 0",
            "  notes of a note type with no link ID field, left out: 1",
            f'Note type "{VOCAB}", link ID field "Word ID", notes selected: 4',
            f"    {GENERATED}: {self.original.id} keeps it (the oldest holder);"
            f" {self.copy.id} -> {self.copy.id} (its own note id)",
            f"    {self.placeholder_holder.id}: '{PLACEHOLDER}'",
            f"  {self.sentence.id}: eat, {PLACEHOLDER} -> {self.placeholder_holder.id}",
            f"  {self.sentence.id}: run, {UNHELD} -> match, no note holds it",
            f'  "{OTHER}", notes selected: 1',
        ]:
            self.assertIn(line + "\n", report)
        self.assertNotIn("Nothing to write", report)


class OneTypeFillTests(unittest.TestCase):
    def test_a_placeholder_several_notes_hold_is_left_and_their_fields_are_filled(self):
        col = Collection(ONE_TYPE)
        first = col.add(VOCAB, **{"Sort": "eat", "Word ID": str(PLACEHOLDER), "Words": "[]"})
        # A copy of it, made while its field still held the placeholder
        second = col.add(VOCAB, **{"Sort": "eat", "Word ID": str(PLACEHOLDER), "Words": "[]"})
        array = words(word("eat", [PLACEHOLDER]), word("dog", [first.id, 2]))
        sentence = col.add(VOCAB, own_id=True, **{"Sort": "dog", "Words": array})

        plan = col.plan()

        # Which of the two the word meant is not known: the report names both
        self.assertEqual(
            plan.writes(),
            {first.id: {"Word ID": str(first.id)}, second.id: {"Word ID": str(second.id)}},
        )
        self.assertEqual(
            [(p.note_id, p.placeholder, p.holders) for p in plan.placeholders],
            [(sentence.id, PLACEHOLDER, [first.id, second.id])],
        )
        self.assertIn(
            f"  {sentence.id}: eat, {PLACEHOLDER} -> left, held by notes {first.id}, {second.id}\n",
            fill.report_text(plan),
        )
        # Once their fields hold their note ids, no note holds the placeholder: a second run
        # matches the word again, as the match op's own resolving would
        col.write(plan)
        self.assertEqual(
            col.plan().writes(),
            {sentence.id: {"Words": words(word("eat", ["match"]), word("dog", [first.id, 2]))}},
        )

    def test_an_array_that_does_not_read_is_left_as_it_is(self):
        col = Collection(ONE_TYPE)
        col.add(VOCAB, own_id=True, **{"Sort": "a", "Words": f'[["x", "noun", "x", "x", [{UNHELD}'})
        col.add(VOCAB, own_id=True, **{"Sort": "b", "Words": ""})

        plan = col.plan()

        self.assertEqual(plan.writes(), {})
        self.assertEqual(plan.placeholders, [])


if __name__ == "__main__":
    unittest.main()
