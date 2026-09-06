"""One whole-collection scan per note id, however many times the run asks about it.

Two halves. `SentenceCacheTests` covers the cache on its own, loaded the stdlib-only way; the
rest drives the real `get_sentences_for_note` with the collection faked at its own seam, which
is where the property that matters lives - what is cached is the *other* notes' sentences, so
the two call shapes cannot poison each other.
"""

import threading
import unittest

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
import addon_modules  # noqa: F401
from addon_modules import load_addon_module
from anki_stubs import load_ops_module

sc = load_addon_module("sentence_cache")
cm = load_ops_module("clean_meaning")


class SentenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = sc.SentenceCache()
        self.scans: list[int] = []

    def scan_for(self, note_id, sentences=None):
        def scan():
            self.scans.append(note_id)
            return sentences if sentences is not None else [{"jp_sentence": str(note_id)}]

        return self.cache.get(note_id, scan)

    def test_the_first_ask_scans_and_returns_what_the_scan_found(self):
        self.assertEqual(self.scan_for(7), [{"jp_sentence": "7"}])
        self.assertEqual(self.scans, [7])

    def test_the_second_ask_for_the_same_note_does_not_scan_again(self):
        self.scan_for(7)
        self.assertEqual(self.scan_for(7), [{"jp_sentence": "7"}])
        self.assertEqual(self.scans, [7])

    def test_a_different_note_is_its_own_question(self):
        self.scan_for(7)
        self.scan_for(8)
        self.assertEqual(self.scans, [7, 8])

    def test_an_empty_answer_is_remembered_like_any_other(self):
        # A note nothing else mentions is the commonest answer there is, and re-asking it costs
        # exactly what re-asking a full one costs
        self.assertEqual(self.scan_for(7, sentences=[]), [])
        self.assertEqual(self.scan_for(7, sentences=[]), [])
        self.assertEqual(self.scans, [7])

    def test_two_threads_asking_at_once_scan_once_between_them(self):
        """The callers arrive from asyncio.to_thread with several hundred tasks in flight.

        Without the per-id lock both would find the key absent and both would run a 0.389s
        whole-collection pass.
        """
        started = threading.Event()
        release = threading.Event()

        def scan():
            self.scans.append(7)
            started.set()
            release.wait(5)
            return [{"jp_sentence": "7"}]

        results: list = []
        threads = [
            threading.Thread(target=lambda: results.append(self.cache.get(7, scan)))
            for _ in range(2)
        ]
        threads[0].start()
        started.wait(5)
        threads[1].start()
        release.set()
        for thread in threads:
            thread.join(5)

        self.assertEqual(self.scans, [7])
        self.assertEqual(results, [[{"jp_sentence": "7"}], [{"jp_sentence": "7"}]])

    def test_a_scan_that_fails_is_not_remembered_as_an_answer(self):
        # The same rule the dictionary memo learned the hard way: a failure must not become a
        # cached fact
        def failing():
            self.scans.append(7)
            raise RuntimeError("collection went away")

        with self.assertRaises(RuntimeError):
            self.cache.get(7, failing)
        self.assertEqual(self.scan_for(7), [{"jp_sentence": "7"}])
        self.assertEqual(self.scans, [7, 7])

    def test_it_counts_what_it_avoided(self):
        for _ in range(4):
            self.scan_for(7)
        self.assertEqual((self.cache.asked, self.cache.scanned), (4, 1))
        self.assertEqual(len(self.cache), 1)


CONFIG = {
    "Vocab": {
        "word_list_field": "sentence-vocab-list",
        "sentence_field": "sentence",
        "translated_sentence_field": "translation",
    }
}


class FakeNote:
    def __init__(self, note_id, sentence="", translation=""):
        self.id = note_id
        self.fields = {"sentence": sentence, "translation": translation}

    def note_type(self):
        return {"name": "Vocab"}

    def __getitem__(self, key):
        return self.fields[key]

    def __contains__(self, key):
        return key in self.fields


class GetSentencesForNoteTests(unittest.TestCase):
    """The collection faked at clean_meaning's own seam, so the tests see what reached it."""

    def setUp(self):
        self.searches: list[str] = []
        self.fetches: list[list[int]] = []
        self.notes = {
            2: FakeNote(2, "にほんごの文", "a Japanese sentence"),
            3: FakeNote(3, "もうひとつ", "another one"),
        }
        self.found = [2, 3]

        def fake_find_notes(query):
            self.searches.append(query)
            return list(self.found)

        def fake_get_notes(note_ids):
            ids = list(note_ids)
            self.fetches.append(ids)
            return [self.notes[note_id] for note_id in ids if note_id in self.notes]

        self.real_find, self.real_get = cm.col_find_notes, cm.col_get_notes
        cm.col_find_notes, cm.col_get_notes = fake_find_notes, fake_get_notes
        self.cache = sc.SentenceCache()

    def tearDown(self):
        cm.col_find_notes, cm.col_get_notes = self.real_find, self.real_get

    def sentences(self, note, exclude_self=False, cache=True):
        return cm.get_sentences_for_note(
            CONFIG,
            note,
            exclude_self=exclude_self,
            sentence_cache=self.cache if cache else None,
        )

    def test_the_note_own_sentence_comes_first_and_then_the_others(self):
        note = FakeNote(1, "この文", "this sentence")
        self.assertEqual(
            self.sentences(note),
            [
                {"jp_sentence": "この文", "en_sentence": "this sentence"},
                {"jp_sentence": "にほんごの文", "en_sentence": "a Japanese sentence"},
                {"jp_sentence": "もうひとつ", "en_sentence": "another one"},
            ],
        )

    def test_the_siblings_are_fetched_in_one_turn_rather_than_one_each(self):
        self.sentences(FakeNote(1))
        self.assertEqual(self.fetches, [[2, 3]])

    def test_asking_again_about_the_same_note_does_not_scan_again(self):
        note = FakeNote(1, "この文", "this sentence")
        first = self.sentences(note)
        self.assertEqual(self.sentences(note), first)
        self.assertEqual(len(self.searches), 1)

    def test_the_two_call_shapes_do_not_poison_each_other(self):
        """`exclude_self` decides only whether this note's own sentence goes on the front.

        Caching the returned value rather than the other notes' sentences would serve one
        caller's answer to the other - and the answers differ by exactly the sentence the
        prompt is about.
        """
        note = FakeNote(1, "この文", "this sentence")
        with_self = self.sentences(note, exclude_self=False)
        without_self = self.sentences(note, exclude_self=True)
        self.assertEqual(with_self[0], {"jp_sentence": "この文", "en_sentence": "this sentence"})
        self.assertEqual(without_self, with_self[1:])
        # ...and the order does not matter either
        other_note = FakeNote(4, "よそ", "elsewhere")
        self.found = [2]
        self.assertEqual(
            self.sentences(other_note, exclude_self=True),
            [{"jp_sentence": "にほんごの文", "en_sentence": "a Japanese sentence"}],
        )
        self.assertEqual(
            self.sentences(other_note, exclude_self=False)[0],
            {"jp_sentence": "よそ", "en_sentence": "elsewhere"},
        )

    def test_the_caller_cannot_edit_what_the_cache_holds(self):
        note = FakeNote(1, "この文", "this sentence")
        self.sentences(note).append({"jp_sentence": "extra", "en_sentence": "extra"})
        self.assertEqual(len(self.sentences(note)), 3)

    def test_a_new_note_has_only_its_own_sentence_and_asks_nothing(self):
        # Nothing can list a note that has no id yet
        new_note = FakeNote(0, "あたらしい", "new")
        self.assertEqual(
            self.sentences(new_note),
            [{"jp_sentence": "あたらしい", "en_sentence": "new"}],
        )
        self.assertEqual(self.sentences(new_note, exclude_self=True), [])
        self.assertEqual(self.searches, [])

    def test_without_a_cache_it_scans_every_time_exactly_as_before(self):
        note = FakeNote(1, "この文", "this sentence")
        self.sentences(note, cache=False)
        self.sentences(note, cache=False)
        self.assertEqual(len(self.searches), 2)

    def test_the_search_is_the_one_it_always_was(self):
        self.sentences(FakeNote(1))
        self.assertEqual(self.searches, ['"sentence-vocab-list:*1*" -nid:1'])


if __name__ == "__main__":
    unittest.main()
