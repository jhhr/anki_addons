"""The kanjify golden set's checks: what a labelled row must pass, what a program flags in a
sentence's furigana, how a queue is split between machines and what an agent may run."""

import sys
import unittest
from pathlib import Path

# See the note in test_vocab_morphology.py: the suite already has a `conftest` of its own, so
# the path is set here rather than in one more file by that name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_queue  # noqa: E402
import kanjify_golden as golden  # noqa: E402
import kanjify_golden_render as render  # noqa: E402

BACKSLASH = chr(92)
# The escapes spelled out: a tool that decodes JSON would turn a literal one into kana again
ESCAPED_YORU = BACKSLASH + "u3088" + BACKSLASH + "u308b"
SENTENCE = "これは 本[ほん]です。"


class RowProblemTest(unittest.TestCase):
    def test_a_kanjified_row_passes(self):
        self.assertIsNone(golden.row_problem(SENTENCE, "<k> 此[こ]れ</k>は 本[ほん]です。"))

    def test_an_unchanged_sentence_is_a_label(self):
        # unlike a note edit, a sentence with nothing to kanjify is a valid label
        self.assertIsNone(golden.row_problem(SENTENCE, SENTENCE))

    def test_changed_text_is_refused(self):
        self.assertIsNotNone(golden.row_problem(SENTENCE, "<k> 此[こ]れ</k>が 本[ほん]です。"))

    def test_unpaired_tags_are_refused(self):
        self.assertIn("pair", golden.row_problem(SENTENCE, "<k> 此[こ]れは 本[ほん]です。"))

    def test_a_kanji_changed_outside_the_spans_is_refused(self):
        # reads the same, so edit_problem alone lets it through; the op's reverse check doesn't
        problem = golden.row_problem(SENTENCE, "<k> 此[こ]れ</k>は 書[ほん]です。")
        self.assertIsNotNone(problem)

    def test_a_span_without_kanji_is_refused(self):
        self.assertIn("no kanji", golden.row_problem(SENTENCE, "<k>これ</k>は 本[ほん]です。"))


class FuriganaSuspectsTest(unittest.TestCase):
    def test_clean_sentence(self):
        self.assertEqual(golden.furigana_suspects("<b> 三[みっ]つ</b> 買[か]った。"), [])

    def test_no_space_after_kana(self):
        found = golden.furigana_suspects(" 三[みっ]つ買[か]った。")
        self.assertEqual(len(found), 1)
        self.assertIn("つ買[か]", found[0])

    def test_a_group_after_a_tag_is_fine(self):
        self.assertEqual(golden.furigana_suspects("<b>つ</b>買[か]った。"), [])

    def test_ke_in_a_counter_is_fine(self):
        self.assertEqual(golden.furigana_suspects(" 一ヶ月[いっかげつ]"), [])

    def test_a_missing_space_is_put_back(self):
        self.assertEqual(golden.fix_spaces(" 三[みっ]つ買[か]った。"), " 三[みっ]つ 買[か]った。")
        self.assertEqual(golden.furigana_suspects(golden.fix_spaces("はまだ終[お]わる")), [])

    def test_a_right_sentence_is_left_alone(self):
        for s in ("<b>つ</b>買[か]った", " 一ヶ月[いっかげつ]", " 本[ほん]です"):
            self.assertEqual(golden.fix_spaces(s), s)

    def test_several_readings(self):
        found = golden.furigana_suspects(" 額[がく, ひたい]は")
        self.assertEqual(len(found), 1)

    def test_kanji_with_no_reading(self):
        found = golden.furigana_suspects("祁門 紅茶[こうちゃ]")
        self.assertEqual(found, ["kanji with no reading: 祁門"])


class ShardTest(unittest.TestCase):
    def test_every_item_is_in_exactly_one_shard(self):
        ids = [f"b{n:04d}" for n in range(200)]
        for item in ids:
            hits = [i for i in range(3) if agent_queue.in_shard(item, (i, 3))]
            self.assertEqual(len(hits), 1)

    def test_bad_shard(self):
        with self.assertRaises(Exception):
            agent_queue.parse_shard("3/3")


class CommandTest(unittest.TestCase):
    def test_a_lookup_agent_may_run_the_lookup_script_only(self):
        cmd = agent_queue.command({"tools": "lookup", "effort": "high"}, "claude", "py -3.10")
        rules = cmd[cmd.index("--allowed-tools") + 1 : cmd.index("--permission-mode")]
        self.assertEqual(rules, ["Bash(py -3.10 word_array/research/kanjify_lookup.py:*)"])
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Bash")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")

    def test_no_tools(self):
        cmd = agent_queue.command({"tools": "none"}, "claude", "python3")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--allowed-tools", cmd)
        self.assertNotIn("--effort", cmd)

    def test_the_schema_goes_on_the_command_line_as_ascii(self):
        cmd = agent_queue.command({"schema": "batch_schema.json"}, "claude", "python3")
        self.assertTrue(cmd[cmd.index("--json-schema") + 1].isascii())

    def test_the_machine_fills_the_python_command(self):
        self.assertEqual(agent_queue.fill("run {PYTHON} x.py", "py -3.10"), "run py -3.10 x.py")


class SampleUsesTest(unittest.TestCase):
    def test_a_rare_spelling_is_not_crowded_out(self):
        uses = [{"kind": "kanjified", "kanji": "依"}] * 50 + [{"kind": "kanjified", "kanji": "由"}]
        uses += [{"kind": "kana", "kanji": ""}] * 30
        sample = render.sample_uses(uses, 10)
        self.assertEqual(len(sample), 10)
        self.assertIn("由", {u["kanji"] for u in sample})
        self.assertIn("kana", {u["kind"] for u in sample})


class TemplatesTest(unittest.TestCase):
    def test_the_escape_example_is_an_escape(self):
        # Written through a tool that decodes JSON, the example once became the kana it stands
        # for, and the agents were told "write Japanese as escapes (yoru is <yoru in kana>)"
        for name in ("word_template.md", "batch_template.md"):
            text = (golden.AGENTS_DIR / name).read_text(encoding="utf-8")
            self.assertIn("`" + ESCAPED_YORU + "`", text, name)

    def test_no_template_names_a_machines_folders(self):
        for path in Path(golden.AGENTS_DIR).parent.glob("*_agents/*.md"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"[A-Z]:\\\\|/home/|/Users/", path.name)


if __name__ == "__main__":
    unittest.main()
