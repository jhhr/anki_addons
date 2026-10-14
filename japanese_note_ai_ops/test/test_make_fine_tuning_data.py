import json
import types
import unittest
from unittest import mock

from addon_modules import load_ops_module, mw

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


if __name__ == "__main__":
    unittest.main()


class ExportFromTheBrowserTests(unittest.TestCase):
    """The browser's "Export kanjify test data": the writer's message, posted under the
    command's name."""

    def setUp(self):
        self.posts: list = []
        self.config: object = {"kanji_sentence_fine_tuning_data_query": "deck:A"}
        for target, name, value in (
            (make_fine_tuning_data, "post", lambda **fields: self.posts.append(fields)),
            (mw, "addonManager", types.SimpleNamespace(getConfig=lambda _: self.config)),
        ):
            patcher = mock.patch.object(target, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_export(self):
        make_fine_tuning_data._run_with_config(
            lambda config, nids: f"Wrote {len(nids)} rows", [1, 2], "Exported kanjify test data"
        )

    def test_the_writers_message_is_the_detail_under_the_commands_name(self):
        self.run_export()
        [post] = self.posts
        self.assertEqual(
            (post["title"], post["body"]), ("Exported kanjify test data", "Wrote 2 rows")
        )
        self.assertEqual((post["source"], post["level"]), ("Japanese Note AI Ops", "success"))

    def test_without_a_config_nothing_is_written_or_posted(self):
        self.config = None
        self.run_export()
        self.assertEqual(self.posts, [])
