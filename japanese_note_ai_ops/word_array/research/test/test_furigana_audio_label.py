"""The furigana audio eval's labelling by ear: the readings each word offers, the queue the op's
rule makes of the scored runs, the session that keeps the labels, and the server the page talks
to. The scorer's use of the labels is tested with the rest of it, in test_furigana_audio.py."""

import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

# See the note in test_vocab_morphology.py: the suite already has a `conftest` of its own, so
# the path is set here rather than in one more file by that name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import furigana_audio as fa  # noqa: E402
import furigana_audio_label as label  # noqa: E402


def heard(sudachi, heard_kana, op_hears, card="c1", line=0, start=0, gold=None, clean=True):
    """One model's row of a word, as furigana_audio_score.py writes it."""
    return {
        "id": card,
        "line": line,
        "start": start,
        "surface": "他",
        "sudachi": sudachi,
        "caption": gold,
        "kinds": [],
        "heard": heard_kana,
        "clean": clean,
        "gold": gold,
        "gold_is_draft": None,
        "hears": {},
        "op_hears": op_hears,
    }


class ChoicesTest(unittest.TestCase):
    def test_readings_that_sound_alike_are_one_choice_spelt_as_the_draft(self):
        rows = [
            heard("とうきょう", "トーキョー", {"draft": True}),
            heard("とうきょう", "トオキョウ", {"draft": True}),
        ]
        self.assertEqual(
            label.choices(rows, ["a", "b"]), [{"kana": "とうきょう", "from": ["draft", "a", "b"]}]
        )

    def test_a_reading_the_dictionary_has_and_a_models_own_hearing(self):
        rows = [
            heard("なん", "ナニ", {"draft": False, "なに": True}),
            heard("なん", "ナデ", {"draft": False, "なに": False}),
        ]
        self.assertEqual(
            label.choices(rows, ["a", "b"]),
            [
                {"kana": "なで", "from": ["b"]},
                {"kana": "なに", "from": ["dictionary", "a"]},
                {"kana": "なん", "from": ["draft"]},
            ],
        )

    def test_a_draft_sudachi_could_not_read_is_not_offered(self):
        rows = [heard("すい苓", "スイレイ", {"draft": False})]
        self.assertEqual(label.choices(rows, ["a"]), [{"kana": "すいれい", "from": ["a"]}])


class QueueTest(unittest.TestCase):
    """The words worth an ear, by why the rule wrote them as it did."""

    def build(self, sample):
        draft = {"draft": True, "た": False}
        other = {"draft": False, "た": True}
        neither = {"draft": False, "た": False}
        a = [
            heard("ほか", "タ", other, "c1"),  # both hear another reading: a correction
            heard("ほか", "ネコ", neither, "c1", start=2, gold="ねこ"),  # the caption reads it
            heard("ほか", "ホカ", draft, "c2"),  # both hear the draft
            heard("ほか", "ホカ", draft, "c3", clean=False),  # a card not matching its line
            heard("ほか", "ホカ", draft, "c4"),  # one hears the draft, the other nothing
            heard("ほか", "ナニ", neither, "c5"),  # neither: a review
            heard("ほか", "ホカ", draft, "c5", start=2),  # both hear the draft, not asked
        ]
        b = [dict(w) for w in a]
        b[4] = heard("ほか", "", neither, "c4")
        word = {"line": 0, "start": 0, "end": 1, "surface": "他", "caption": None}
        neko = {**word, "start": 2, "end": 3, "surface": "猫"}
        selection = [
            {
                "id": card,
                "audio": f"{card}.opus",
                "en": "-",
                "lines": ["他に猫(ねこ)" if card == "c1" else "他に猫"],
                "words": [word]
                + ([{**neko, "caption": "ねこ"}] if card == "c1" else [])
                + ([neko] if card == "c5" else []),
            }
            for card in ("c1", "c2", "c3", "c4", "c5")
        ]
        with tempfile.TemporaryDirectory() as d:
            here = Path(d)
            fa.write_jsonl(here / "a.words.jsonl", a)
            fa.write_jsonl(here / "b.words.jsonl", b)
            fa.write_jsonl(here / "selection.jsonl", selection)
            with unittest.mock.patch.object(label, "RUNS", here):
                with unittest.mock.patch.object(fa, "SELECTION", here / "selection.jsonl"):
                    return label.build_queue(["a", "b"], quorum=1, sample=sample)

    def test_corrections_reviews_and_partial_drafts_first_known_and_unmatched_never(self):
        cards = self.build(sample=0.0)
        self.assertEqual([(c["id"], c["bucket"]) for c in cards], [
            ("c1", "correction"), ("c5", "review"), ("c4", "partial"),
        ])
        self.assertEqual(len(cards[0]["words"]), 1, "the caption-read word is left out")
        self.assertEqual(cards[0]["lines"], [{"text": "他に猫", "ruby": [[2, 3, "ねこ", "caption"]]}])

    def test_a_word_not_asked_about_shows_the_reading_the_rule_writes(self):
        c5 = next(c for c in self.build(sample=0.0) if c["id"] == "c5")
        self.assertEqual([w["start"] for w in c5["words"]], [0])
        self.assertEqual(c5["lines"], [{"text": "他に猫", "ruby": [[2, 3, "ほか", "rule"]]}])

    def test_the_drafts_every_model_heard_are_sampled(self):
        self.assertEqual([c["id"] for c in self.build(sample=1.0)][-1], "c2")
        self.assertNotIn("c2", [c["id"] for c in self.build(sample=0.0)])

    def test_a_words_place_in_the_sample_is_its_own(self):
        # c2's agreed word is in the sample by its own draw, whatever the other cards hold
        draw = label.stable_random(0, "c2", 0, 0, "他")
        self.assertIn("c2", [c["id"] for c in self.build(sample=draw + 1e-9)])
        self.assertNotIn("c2", [c["id"] for c in self.build(sample=draw)])
        self.assertEqual(draw, label.stable_random(0, "c2", 0, 0, "他"))

    def test_the_summary_counts_words_by_bucket(self):
        text = label.queue_summary(self.build(sample=1.0))
        self.assertEqual(
            text, "4 cards, 5 words to label: correction 1, review 1, partial 1, agreed 2"
        )


def queue():
    word = {"surface": "他", "bucket": "review", "choices": [{"kana": "ほか", "from": ["draft"]}]}
    return [
        {
            "id": "c1",
            "audio": "c1.opus",
            "en": "-",
            "lines": [{"text": "他に他", "ruby": []}],
            "bucket": "review",
            "words": [
                {**word, "line": 0, "start": 0, "end": 1},
                {**word, "line": 0, "start": 2, "end": 3},
            ],
        },
        {
            "id": "c2",
            "audio": "c2.opus",
            "en": "-",
            "lines": [{"text": "他", "ruby": []}],
            "bucket": "review",
            "words": [{**word, "line": 0, "start": 0, "end": 1}],
        },
    ]


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "labels.jsonl"
        self.session = label.Session(queue(), self.path)

    def post(self, path, **body):
        return self.session.handle(path, body)

    def test_a_card_stays_until_each_of_its_words_is_labelled(self):
        data = self.post("/api/next")
        self.assertEqual(data["card"]["id"], "c1")
        data = self.post("/api/label", id="c1", line=0, start=0, verdict="heard", reading="ホカ")
        self.assertEqual(data["card"]["id"], "c1")
        self.assertEqual(data["card"]["words"][0]["label"]["reading"], "ほか")
        data = self.post("/api/label", id="c1", line=0, start=2, verdict="unsure")
        self.assertEqual(data["card"]["id"], "c2")
        self.assertEqual(data["stats"]["labelled"], 2)
        rows = fa.read_jsonl(self.path)
        self.assertEqual([(r["start"], r["reading"], r["verdict"]) for r in rows], [
            (0, "ほか", "heard"), (2, None, "unsure"),
        ])
        self.assertFalse(self.path.with_suffix(".jsonl.tmp").exists())

    def test_a_heard_label_needs_a_reading(self):
        self.post("/api/label", id="c1", line=0, start=0, verdict="heard", reading=" ")
        self.assertEqual(fa.read_jsonl(self.path), [])

    def test_undo_takes_back_the_last_label_and_shows_its_word(self):
        self.post("/api/label", id="c1", line=0, start=0, verdict="heard", reading="ほか")
        self.post("/api/label", id="c1", line=0, start=2, verdict="not_said")
        data = self.post("/api/undo")
        self.assertEqual((data["card"]["id"], data["active"]), ("c1", [0, 2]))
        self.assertEqual(len(fa.read_jsonl(self.path)), 1)

    def test_a_skipped_card_comes_back_in_the_next_session_only(self):
        data = self.post("/api/skip", id="c1")
        self.assertEqual(data["card"]["id"], "c2")
        again = label.Session(queue(), self.path)
        self.assertEqual(again.handle("/api/next", {})["card"]["id"], "c1")

    def test_a_label_given_to_another_word_at_its_place_asks_again(self):
        fa.write_jsonl(self.path, [
            {"id": "c2", "line": 0, "start": 0, "surface": "七", "reading": "な", "verdict": "heard"}
        ])
        session = label.Session(queue(), self.path)
        self.assertEqual(session.stats()["labelled"], 0)
        data = session.handle("/api/skip", {"id": "c1"})
        self.assertIsNone(data["card"]["words"][0]["label"])
        session.handle("/api/label", {"id": "c2", "line": 0, "start": 0, "verdict": "heard",
                                      "reading": "ほか"})
        self.assertEqual([r["surface"] for r in fa.read_jsonl(self.path)], ["他"])

    def test_labels_given_before_are_kept(self):
        self.post("/api/label", id="c2", line=0, start=0, verdict="heard", reading="た")
        again = label.Session(queue(), self.path)
        self.assertEqual(again.stats()["labelled"], 1)
        self.assertEqual(again.handle("/api/next", {})["card"]["id"], "c1")


class ServerTest(unittest.TestCase):
    """The page, the clips with their byte ranges, and the API, over a socket on localhost."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.audio = Path(d.name)
        (self.audio / "c1.opus").write_bytes(b"0123456789")
        (self.audio / "secret.txt").write_bytes(b"no")
        session = label.Session(queue(), self.audio / "labels.jsonl")
        handler = label.make_handler(session, self.audio)
        self.server = label.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path, headers=None):
        request = urllib.request.Request(self.url + path, headers=headers or {})
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, dict(r.headers), r.read()

    def test_the_page(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Furigana by ear", body.decode("utf-8"))

    def test_a_clip_whole_and_a_range_of_it(self):
        status, headers, body = self.get("/audio/c1.opus")
        self.assertEqual((status, headers["Content-Type"], body), (200, "audio/ogg", b"0123456789"))
        status, headers, body = self.get("/audio/c1.opus", {"Range": "bytes=2-5"})
        self.assertEqual((status, headers["Content-Range"], body), (206, "bytes 2-5/10", b"2345"))
        status, headers, body = self.get("/audio/c1.opus", {"Range": "bytes=-3"})
        self.assertEqual(body, b"789")

    def test_only_the_queues_clips(self):
        for path in ("/audio/secret.txt", "/audio/..%2Fsecret.txt", "/audio/c2.opus"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, 404)

    def test_the_api(self):
        body = json.dumps({"id": "c2", "line": 0, "start": 0, "verdict": "heard", "reading": "た"})
        request = urllib.request.Request(self.url + "/api/label", data=body.encode("utf-8"))
        with urllib.request.urlopen(request, timeout=5) as r:
            data = json.loads(r.read())
        self.assertEqual(data["card"]["id"], "c1")
        self.assertEqual(fa.read_jsonl(self.audio / "labels.jsonl")[0]["reading"], "た")


if __name__ == "__main__":
    unittest.main()
