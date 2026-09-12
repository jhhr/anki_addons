import json
import unittest

from addon_modules import load_ops_module

make_fine_tuning_data = load_ops_module("make_fine_tuning_data", subdir="sync_local_ops")


class BuildMigrationRowsTests(unittest.TestCase):
    def test_a_row_holds_the_sentence_and_the_raw_word_list(self):
        rows, duplicates = make_fine_tuning_data.build_migration_rows(
            [("猫[ねこ]", '{"nouns": [["猫", "ねこ", 5]]}')]
        )
        self.assertEqual(duplicates, 0)
        self.assertEqual(
            json.loads(rows[0]),
            {"sentence": "猫[ねこ]", "word_list": '{"nouns": [["猫", "ねこ", 5]]}'},
        )

    def test_invalid_json_is_kept_as_it_is(self):
        rows, _ = make_fine_tuning_data.build_migration_rows([("a", '{"nouns": [')])
        self.assertEqual(json.loads(rows[0])["word_list"], '{"nouns": [')

    def test_a_repeated_sentence_keeps_the_first_word_list(self):
        rows, duplicates = make_fine_tuning_data.build_migration_rows(
            [("a", "{}"), ("b", "{}"), ("a", '{"nouns": []}')]
        )
        self.assertEqual(duplicates, 1)
        self.assertEqual([json.loads(r) for r in rows][0], {"sentence": "a", "word_list": "{}"})
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
