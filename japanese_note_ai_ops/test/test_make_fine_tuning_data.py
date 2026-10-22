import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from addon_modules import load_ops_module

make_fine_tuning_data = load_ops_module("make_fine_tuning_data", subdir="sync_local_ops")


class BuildKanjifyRowsTests(unittest.TestCase):
    def test_a_row_holds_both_sentences_and_every_note_id(self):
        rows, duplicates = make_fine_tuning_data.build_kanjify_rows(
            [(1, "ねこ", "<k>猫[ねこ]</k>"), (2, "ねこ", "猫")]
        )
        self.assertEqual(duplicates, 1)
        self.assertEqual(
            [json.loads(r) for r in rows],
            [{"sentence": "ねこ", "kanjified": "<k>猫[ねこ]</k>", "nids": [1, 2]}],
        )


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
            ["kanji_sentence_fine_tuning_data_query"],
        )



class Note:
    def __init__(self, furigana: str, tags=()) -> None:
        self.fields = {"Furigana": furigana, "Kanjified": "<k> 他[た]</k>の"}
        self.tags = list(tags)

    def note_type(self):
        return {"name": "Sentence note"}

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]


class KanjifyExportTests(unittest.TestCase):
    def test_a_note_whose_furigana_awaits_a_check_is_left_out_and_counted(self):
        # Its ？ words are no sentence, and its kanjified one is the furigana's it replaced
        config = {
            "Sentence note": {
                "furigana_sentence_field": "Furigana",
                "kanjified_sentence_field": "Kanjified",
            }
        }
        notes = {
            1: Note(" 他[た]の"),
            2: Note(" 他[ほか？た]の"),
            3: Note(" 他[た]の", tags=["furigana-review"]),
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            make_fine_tuning_data, "_OUTPUT_DIR", directory
        ), mock.patch.object(make_fine_tuning_data, "mw") as mw:
            mw.col.get_note.side_effect = notes.__getitem__
            message = make_fine_tuning_data._write_kanjify_sentence_data(config, [1, 2, 3])
            rows = (Path(directory) / "kanjify_sentence_data.jsonl").read_text(encoding="utf-8")
        self.assertEqual([json.loads(row)["nids"] for row in rows.splitlines()], [[1]])
        self.assertTrue(message.endswith("Left out 2 notes whose furigana awaits a check."))


if __name__ == "__main__":
    unittest.main()
