"""The note-level word-array migration operation."""

import sys
import unittest
from types import ModuleType
from unittest import mock

from addon_modules import load_ops_module

sudachipy = ModuleType("sudachipy")
setattr(sudachipy, "Dictionary", object)
setattr(sudachipy, "SplitMode", object)
with mock.patch.dict(sys.modules, {"sudachipy": sudachipy}):
    migrate_word_arrays = load_ops_module("migrate_word_arrays", subdir="sync_local_ops")


class FakeNote:
    def __init__(self, fields, tags):
        self.id = 1
        self.fields = fields
        self.tags = set(tags)

    def note_type(self):
        return {"name": "Sentence"}

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]

    def has_tag(self, tag):
        return tag in self.tags


class MigrateWordArrayInNoteTests(unittest.TestCase):
    def test_a_note_with_the_migrated_tag_is_left_alone(self):
        note = FakeNote(
            {"sentence": "本を読む。", "words": '[["本"]]'},
            {migrate_word_arrays.MIGRATED_TAG},
        )
        updates = {}

        fields = {
            "word_extraction_sentence_field": "sentence",
            "word_list_field": "words",
        }
        with (
            mock.patch.object(
                migrate_word_arrays,
                "get_field_config",
                side_effect=lambda _config, key, _note_type: fields[key],
            ),
            mock.patch.object(migrate_word_arrays.generator, "generate") as generate,
            mock.patch.object(migrate_word_arrays, "decode_word_list_field") as decode,
        ):
            migrated = migrate_word_arrays.migrate_word_array_in_note({}, note, {}, updates)

        self.assertFalse(migrated)
        generate.assert_not_called()
        decode.assert_not_called()
        self.assertEqual(updates, {})
        self.assertEqual(note["words"], '[["本"]]')


if __name__ == "__main__":
    unittest.main()
