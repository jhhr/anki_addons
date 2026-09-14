"""The hand judge's note access: export note ids and the AnkiConnect client, with a fake opener."""

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import anki_connect  # noqa: E402
import hand_labels  # noqa: E402
import note_edits  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def client_replying(reply: dict) -> tuple[anki_connect.AnkiConnect, list[dict]]:
    requests: list[dict] = []

    def opener(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(json.dumps(reply).encode("utf-8"))

    return anki_connect.AnkiConnect(opener=opener), requests


class AnkiConnectTests(unittest.TestCase):
    def test_a_request_names_the_action_version_and_params(self):
        info = [{"noteId": 5, "modelName": "m", "fields": {}}]
        client, requests = client_replying({"result": info, "error": None})
        self.assertEqual(client.notes_info([5]), info)
        self.assertEqual(
            requests, [{"action": "notesInfo", "version": 6, "params": {"notes": [5]}}]
        )

    def test_update_note_fields_sends_the_note(self):
        client, requests = client_replying({"result": None, "error": None})
        client.update_note_fields(5, {"f": "新しい"})
        self.assertEqual(requests[0]["params"], {"note": {"id": 5, "fields": {"f": "新しい"}}})

    def test_browse_every_note_of_a_sentence(self):
        client, requests = client_replying({"result": [1], "error": None})
        client.gui_browse(anki_connect.nids_query([1, 2]))
        self.assertEqual(requests[0]["params"], {"query": "nid:1 or nid:2"})

    def test_an_action_error_raises(self):
        client, _ = client_replying({"result": None, "error": "note was not found: 5"})
        with self.assertRaisesRegex(anki_connect.AnkiConnectError, "note was not found"):
            client.update_note_fields(5, {"f": ""})

    def test_anki_not_running_says_so(self):
        def refused(request, timeout):
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

        client = anki_connect.AnkiConnect(opener=refused)
        with self.assertRaisesRegex(anki_connect.AnkiConnectError, "is Anki running"):
            client.version()


class ConfigTests(unittest.TestCase):
    def test_meta_config_over_config_json_names_the_sentence_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps({"Note": {"word_extraction_sentence_field": "old"}}), encoding="utf-8"
            )
            (root / "meta.json").write_text(
                json.dumps({"config": {"Note": {"word_extraction_sentence_field": "s"}}}),
                encoding="utf-8",
            )
            config = anki_connect.load_config(root)
        self.assertEqual(anki_connect.sentence_field(config, "Note"), "s")
        info = {
            "noteId": 1,
            "modelName": "Note",
            "fields": {"s": {"value": "猫[ねこ]", "order": 0}},
        }
        self.assertEqual(anki_connect.note_sentence(config, info), "猫[ねこ]")
        with self.assertRaisesRegex(anki_connect.AnkiConnectError, "Other"):
            anki_connect.sentence_field(config, "Other")
        with self.assertRaisesRegex(anki_connect.AnkiConnectError, "no field"):
            anki_connect.note_sentence(config, {"noteId": 2, "modelName": "Note", "fields": {}})


class ExportNidsTests(unittest.TestCase):
    def test_note_ids_by_raw_sentence(self):
        rows = [
            {"sentence": "<i>前</i>猫[ねこ]", "word_list": "{}", "nids": [1, 2]},
            {"sentence": "犬[いぬ]", "word_list": "{}"},
            {"sentence": "<i>前</i>猫[ねこ]", "word_list": "{}", "nids": [2, 3]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.jsonl"
            text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n\n"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(hand_labels.read_export_nids(path), {"<i>前</i>猫[ねこ]": [1, 2, 3]})
            self.assertEqual(hand_labels.read_export_nids(Path(tmp) / "missing.jsonl"), {})


class UnkanjifyTests(unittest.TestCase):
    def test_a_span_keeps_only_its_furigana_readings(self):
        field = "<k> 此[こ]の</k> 本[ほん]を<k> 下[くだ]さい</k>。"
        self.assertEqual(note_edits.unkanjify(field, 0), "この 本[ほん]を<k> 下[くだ]さい</k>。")
        self.assertEqual(note_edits.unkanjify(field, 1), "<k> 此[こ]の</k> 本[ほん]をください。")
        self.assertEqual(note_edits.unkanjify("<k>奴[ヤツ]</k>", 0), "ヤツ")
        self.assertEqual(
            note_edits.unkanjify("<k><b> 間[ま]も 無[な]く</b></k>", 0), "<b>まもなく</b>"
        )

    def test_spans_are_counted_outside_the_context(self):
        field = "<i><k> 其[そ]の</k>日[ひ]。</i><k> 此[こ]の</k> 日[ひ]<i><k> 彼[あ]の</k></i>"
        self.assertEqual(
            note_edits.unkanjify(field, 0),
            "<i><k> 其[そ]の</k>日[ひ]。</i>この 日[ひ]<i><k> 彼[あ]の</k></i>",
        )
        with self.assertRaisesRegex(note_edits.NoteEditError, "no <k> span number 2"):
            note_edits.unkanjify(field, 1)

    def test_a_span_without_furigana_or_unclosed_is_refused_but_counted(self):
        field = "<k>この</k><k> 為[し]ます<k> 此[こ]れ</k>"
        self.assertEqual(note_edits.changes(field), [None, None, ("此[こ]れ", "これ")])
        for k in (0, 1):
            with self.assertRaises(note_edits.NoteEditError):
                note_edits.unkanjify(field, k)
        self.assertEqual(note_edits.unkanjify(field, 2), "<k>この</k><k> 為[し]ますこれ")


def word(raw: str, dict_form: str, reading: str, pos: str = "noun") -> list:
    return [raw, pos, dict_form, reading, [], []]


def fake_generate(sentence: str, lexicon: dict) -> list:
    """An array of one noun per character, each read as itself."""
    return [word(ch, ch, ch) for ch in sentence]


def label(sentence: str, form: str, value: str = "match") -> dict:
    return {
        "sentence": sentence,
        "path": [form],
        "occurrence": 0,
        "reading": form,
        "pos": "noun",
        "group": "noun-main",
        "label": value,
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class MoveLabelsTests(unittest.TestCase):
    def test_labels_follow_the_words_the_new_array_still_has(self):
        labels = [
            label("犬猫", "犬"),
            label("犬猫", "猫"),
            label("鳥", "鳥"),
            label("犬鳥", "鳥", "x"),
        ]
        out, moved, dropped = hand_labels.move_labels(
            labels, "犬猫", "犬鳥", fake_generate("犬鳥", {})
        )
        self.assertEqual((moved, dropped), (1, 1))
        self.assertEqual(
            sorted((r["sentence"], r["path"][0], r["label"]) for r in out),
            [("犬鳥", "犬", "match"), ("犬鳥", "鳥", "x"), ("鳥", "鳥", "match")],
        )


class RewriteRowsTests(unittest.TestCase):
    def test_edited_notes_rows_take_the_new_raw_sentence(self):
        with tempfile.TemporaryDirectory() as tmp:
            export, checked = Path(tmp) / "export.jsonl", Path(tmp) / "checked.jsonl"
            write_jsonl(
                export,
                [
                    {"sentence": "<i>前</i>犬", "word_list": "a", "nids": [1, 2]},
                    {"sentence": "猫", "word_list": "b", "nids": [3]},
                    {"sentence": "鳥", "word_list": "c", "nids": [4]},
                ],
            )
            write_jsonl(checked, [{"sentence": "<i>前</i>犬", "word_list": "a"}])
            new_raw = {1: "<i>前</i>猫", 4: "猫"}
            renamed = {"<i>前</i>犬": "<i>前</i>猫", "鳥": "猫"}
            self.assertEqual(hand_labels.rewrite_rows(export, new_raw, renamed), 2)
            self.assertEqual(hand_labels.rewrite_rows(checked, new_raw, renamed), 1)
            self.assertEqual(hand_labels.rewrite_rows(Path(tmp) / "none", new_raw, renamed), 0)
            self.assertEqual(
                read_jsonl(export),
                [
                    {"sentence": "<i>前</i>猫", "word_list": "a", "nids": [1]},
                    {"sentence": "<i>前</i>犬", "word_list": "a", "nids": [2]},
                    {"sentence": "猫", "word_list": "b", "nids": [3, 4]},
                ],
            )
            self.assertEqual(read_jsonl(checked), [{"sentence": "<i>前</i>猫", "word_list": "a"}])


try:
    import hand_judge  # noqa: E402
except ImportError as e:  # SudachiPy-less Python: the generator module can't load
    hand_judge = None
    HAND_JUDGE_ERROR = str(e)


class FakeAnki:
    def __init__(self, fields: dict[int, str]):
        self.fields = fields
        self.browsed: list[str] = []
        self.written: list[tuple[int, dict]] = []

    def update_note_fields(self, nid, fields):
        self.written.append((nid, fields))
        self.fields[nid] = fields["s"]

    def notes_info(self, nids):
        return [
            (
                {"noteId": n, "modelName": "Note", "fields": {"s": {"value": self.fields[n]}}}
                if n in self.fields
                else {}
            )
            for n in nids
        ]

    def gui_browse(self, query):
        self.browsed.append(query)
        return []


CONFIG = {"Note": {"word_extraction_sentence_field": "s"}}


@unittest.skipIf(hand_judge is None, "hand_judge needs the generator's imports")
class SentenceChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.labels_path = self.dir / "labels.jsonl"
        write_jsonl(self.labels_path, [label("犬猫", "犬"), label("犬猫", "猫", "dontmatch")])
        self.export = self.dir / "export.jsonl"
        write_jsonl(self.export, [{"sentence": "<i>前</i>犬猫", "word_list": "w", "nids": [7]}])
        self.session = hand_judge.Session(
            ["牛", "犬猫", "馬"],
            {},
            self.labels_path,
            1,
            nids={"犬猫": [7]},
            raws={7: "<i>前</i>犬猫"},
            generate=fake_generate,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_unchanged_note_says_so(self):
        anki = FakeAnki({7: "<i>別</i>犬猫"})
        message = self.session.refetch("犬猫", anki, CONFIG, [self.export])
        self.assertEqual(message, hand_judge.NO_CHANGE)

    def test_sentence_without_notes_says_so(self):
        self.assertEqual(self.session.refetch("牛", FakeAnki({}), CONFIG, []), hand_judge.NO_NIDS)
        self.assertEqual(self.session.browse("牛", FakeAnki({})), hand_judge.NO_NIDS)

    def test_browse_every_note(self):
        anki = FakeAnki({})
        self.session.nids["犬猫"] = [7, 8]
        self.session.browse("犬猫", anki)
        self.assertEqual(anki.browsed, ["nid:7 or nid:8"])

    def test_edited_sentence_comes_next_with_its_labels_moved(self):
        s = self.session
        self.assertEqual(s.next_candidate({"noun-main"}).sentence, "牛")
        s._generate_more()  # 犬猫 too
        message = s.refetch("犬猫", FakeAnki({7: "<i>前</i>犬鳥"}), CONFIG, [self.export])
        self.assertIn("1 labels moved, 1 dropped, 1 jsonl rows", message)

        self.assertEqual(s.sentences, ["犬鳥", "牛", "馬"])
        self.assertEqual(s.pool[0].sentence, "犬鳥")
        self.assertNotIn("犬猫", {c.sentence for c in s.pool})
        self.assertEqual(s.next_candidate({"noun-main"}).placed.elem[2], "鳥")  # 犬 is labelled
        self.assertEqual(
            [(r["sentence"], r["path"]) for r in hand_labels.read_labels(self.labels_path)],
            [("犬鳥", ["犬"])],
        )
        self.assertEqual(sum(s.placements.values()), 3)  # 犬, 鳥, 牛
        self.assertEqual(s.nids, {"犬鳥": [7]})
        self.assertEqual(read_jsonl(self.export)[0]["sentence"], "<i>前</i>犬鳥")

    def test_sentence_not_generated_yet_is_generated_now(self):
        s = self.session
        s.refetch("犬猫", FakeAnki({7: "犬鳥"}), CONFIG, [])
        self.assertEqual(s.sentences, ["犬鳥", "牛", "馬"])
        self.assertEqual(s.next_sentence, 1)
        self.assertEqual({c.sentence for c in s.pool}, {"犬鳥"})

    def test_notes_that_part_ways_each_get_their_sentence(self):
        s = self.session
        s.nids["犬猫"] = [7, 8]
        s.raws[8] = "犬猫"
        s.refetch("犬猫", FakeAnki({7: "犬鳥", 8: "犬猫"}), CONFIG, [])
        self.assertEqual(s.nids, {"犬鳥": [7], "犬猫": [8]})
        self.assertIn("犬猫", s.sentences)
        self.assertEqual(
            {r["sentence"] for r in hand_labels.read_labels(self.labels_path)}, {"犬鳥"}
        )


@unittest.skipIf(hand_judge is None, "hand_judge needs the generator's imports")
class UnkanjifySessionTests(unittest.TestCase):
    SENTENCE = "犬<k> 猫[ねこ]</k>"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.labels_path = self.dir / "labels.jsonl"
        write_jsonl(self.labels_path, [label(self.SENTENCE, "犬")])
        self.export = self.dir / "export.jsonl"
        raw = "<i>前</i>" + self.SENTENCE
        write_jsonl(self.export, [{"sentence": raw, "word_list": "w", "nids": [7]}])
        self.anki = FakeAnki({7: raw})
        self.session = hand_judge.Session(
            [self.SENTENCE],
            {},
            self.labels_path,
            1,
            nids={self.SENTENCE: [7]},
            raws={7: raw},
            generate=fake_generate,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def labelled(self) -> list[str]:
        return [r["sentence"] for r in hand_labels.read_labels(self.labels_path)]

    def test_span_written_as_kana_then_reverted(self):
        s, anki = self.session, self.anki
        message = s.unkanjify(self.SENTENCE, 0, anki, CONFIG, [self.export])
        self.assertIn("1 labels moved", message)
        self.assertEqual(anki.fields[7], "<i>前</i>犬ねこ")
        self.assertEqual(s.sentences, ["犬ねこ"])
        self.assertEqual(self.labelled(), ["犬ねこ"])
        self.assertEqual(read_jsonl(self.export)[0]["sentence"], "<i>前</i>犬ねこ")

        self.assertTrue(s.revert(anki, CONFIG, [self.export]).startswith(hand_judge.REVERTED))
        self.assertEqual(anki.fields[7], "<i>前</i>" + self.SENTENCE)
        self.assertEqual(s.sentences, [self.SENTENCE])
        self.assertEqual(self.labelled(), [self.SENTENCE])
        self.assertEqual(s.edits, [])
        with self.assertRaisesRegex(hand_judge.Refused, "No edit"):
            s.revert(anki, CONFIG, [])

    def test_note_edited_in_anki_meanwhile_is_left_alone(self):
        s, anki = self.session, self.anki
        anki.fields[7] = "犬猫"
        with self.assertRaisesRegex(hand_judge.Refused, "refetch it first"):
            s.unkanjify(self.SENTENCE, 0, anki, CONFIG, [])
        anki.fields[7] = self.SENTENCE
        s.unkanjify(self.SENTENCE, 0, anki, CONFIG, [])
        anki.fields[7] = "犬ネコ"
        with self.assertRaisesRegex(hand_judge.Refused, "changed in Anki"):
            s.revert(anki, CONFIG, [])
        self.assertEqual(len(anki.written), 1)

    def test_page_numbers_spans_as_the_field_does(self):
        items = [
            ["<k>"],
            [" 此[こ]の</k> 本[ほん]", "noun", "此の本", "このほん", [], []],
            ["を"],
            ["<k>"],
            [" 下[くだ]さい", "verb", "下さる", "くださる", [], []],
            ["</k>"],
        ]
        html = hand_judge.render(items, items[4], [])
        self.assertIn('<span class="k" data-k="0"><ruby>此<rt>こ</rt></ruby>の</span>', html)
        self.assertIn('<mark><span class="k" data-k="1"><ruby>下', html)
        self.assertNotIn('data-k="0"><ruby>本', html)
        self.assertEqual(
            len(note_edits.k_spans("<k> 此[こ]の</k> 本[ほん]を<k> 下[くだ]さい</k>")), 2
        )


if __name__ == "__main__":
    unittest.main()
