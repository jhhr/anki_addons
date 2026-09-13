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


class RunTestDataExportsTests(unittest.TestCase):
    def test_each_export_gets_the_notes_its_query_finds(self):
        calls = []

        def write(name):
            return lambda config, nids: calls.append((name, nids)) or f"{name} done"

        exports = [("a_query", write("a")), ("b_query", write("b")), ("c_query", write("c"))]
        config = {"a_query": "deck:A", "b_query": "  ", "c_query": "tag:c"}
        messages = make_fine_tuning_data.run_test_data_exports(
            config, lambda query: [len(query)], exports
        )
        self.assertEqual(calls, [("a", [6]), ("c", [5])])
        self.assertEqual(messages, ["a done", "Skipped: `b_query` is not set.", "c done"])

    def test_a_failing_query_does_not_stop_the_rest(self):
        def find_notes(query):
            if query == "bad":
                raise ValueError("invalid search")
            return []

        exports = [("a_query", lambda c, n: "a done"), ("b_query", lambda c, n: "b done")]
        messages = make_fine_tuning_data.run_test_data_exports(
            {"a_query": "bad", "b_query": "ok"}, find_notes, exports
        )
        self.assertEqual(messages, ["`a_query` failed: invalid search", "b done"])

    def test_the_config_keys(self):
        self.assertEqual(
            [key for key, _ in make_fine_tuning_data.TEST_DATA_EXPORTS],
            [
                "extract_words_migration_data_query",
                "kanji_sentence_fine_tuning_data_query",
                "extract_words_fine_tuning_data_query",
            ],
        )


if __name__ == "__main__":
    unittest.main()
