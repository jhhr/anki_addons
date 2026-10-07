"""One whole-collection scan per note id, however many times the run asks about it.

Two halves. `SentenceCacheTests` covers the cache on its own, loaded the stdlib-only way; the
rest drives the real `get_sentences_for_note` with the collection faked at its own seam, which
is where the property that matters lives - what is cached is the *other* notes' sentences, so
the two call shapes cannot poison each other. `TwoTypeSentencesTests` does so in the two-type
layout, where a vocab note's own sentence is its example sentence note's.
"""

import copy
import json
import re
import sys
import threading
import unittest
from unittest import mock

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
import addon_modules  # noqa: F401
from addon_modules import load_addon_module
from addon_modules import load_ops_module

sc = load_addon_module("sentence_cache")
cm = load_ops_module("clean_meaning")
# The modules clean_meaning itself imported: the run's note cache reads through the one, and
# the other raises the layout errors
note_cache_module = sys.modules[cm.NoteCache.__module__]
note_roles = sys.modules[cm.sentence_type_of.__module__]


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

    def test_each_other_note_is_listed_once_and_never_the_note_itself(self):
        # Told apart by note id. The check before compared a note's sentence text with the
        # sentences listed so far, which are dicts, and so let every note through
        note = FakeNote(1, "この文", "this sentence")
        self.notes[1] = note
        self.found = [2, 1, 3, 2]
        self.assertEqual(
            [s["en_sentence"] for s in self.sentences(note)],
            ["this sentence", "a Japanese sentence", "another one"],
        )

    def test_two_notes_with_the_same_sentence_are_both_listed(self):
        # By note id, not by text, so the one-type layout's prompts are what they were
        self.notes[3] = FakeNote(3, "にほんごの文", "a Japanese sentence")
        self.assertEqual(
            [s["jp_sentence"] for s in self.sentences(FakeNote(1, "この文", "this"))],
            ["この文", "にほんごの文", "にほんごの文"],
        )

    def test_a_sentence_handed_in_is_the_note_own_in_place_of_its_fields(self):
        given = {"jp_sentence": "渡された<b>文</b>", "en_sentence": "a given sentence"}
        note = FakeNote(1, "この文", "this sentence")
        sentences = cm.get_sentences_for_note(
            CONFIG, note, sentence_cache=self.cache, own_sentence=given
        )
        self.assertEqual(sentences[0], given)
        self.assertEqual(
            [s["en_sentence"] for s in sentences[1:]], ["a Japanese sentence", "another one"]
        )


def array_note(note_id, arr, translation="", word="猫", reading="ねこ"):
    note = FakeNote(note_id, "猫と猫", translation)
    note.fields["sentence-vocab-list"] = json.dumps(arr, ensure_ascii=False)
    note.fields["word"] = word
    note.fields["reading"] = reading
    return note


def cat_array(first_match, second_match):
    return [
        ["猫[ねこ]", "noun", "猫", "ねこ", first_match, []],
        ["と", "particle", "と", "と", ["dontmatch"], []],
        ["猫[ねこ]", "noun", "猫", "ねこ", second_match, []],
    ]


WORD_CONFIG = {
    "Vocab": {
        **CONFIG["Vocab"],
        "word_kanjified_field": "word",
        "word_reading_field": "reading",
    }
}


class HighlightedSentenceTests(GetSentencesForNoteTests):
    """A note holding a word array gives its sentence with the word it links to this note in <b>,
    so a sentence using the word twice says which occurrence the meaning is for."""

    def sentences(self, note, exclude_self=False, cache=True):
        return cm.get_sentences_for_note(
            WORD_CONFIG,
            note,
            exclude_self=exclude_self,
            sentence_cache=self.cache if cache else None,
        )

    def test_the_occurrence_linked_to_the_note_is_marked(self):
        self.notes = {2: array_note(2, cat_array([5, 4], [1, 3]), "cat and cat")}
        self.found = [2]
        note = array_note(1, cat_array([1], [5]), "this")
        self.assertEqual(
            [s["jp_sentence"] for s in self.sentences(note)],
            ["<b>猫</b>と猫", "猫と<b>猫</b>"],
        )

    def test_a_new_note_marks_the_first_occurrence_of_its_word(self):
        new_note = array_note(0, cat_array(["match"], ["match"]))
        self.assertEqual(self.sentences(new_note)[0]["jp_sentence"], "<b>猫</b>と猫")

    def test_a_broken_array_note_keeps_its_sentence_as_it_is(self):
        note = FakeNote(1, "<b>猫</b>と猫", "this")
        note.fields["sentence-vocab-list"] = '[["猫", "noun", "猫", "ねこ", [1], []]'
        self.found = []
        self.assertEqual(self.sentences(note)[0]["jp_sentence"], "猫と猫")


SENTENCE, VOCAB = "Sentence", "Vocab"

# The two blocks name different fields for one key, and the vocab notes keep old sentence
# fields under the sentence block's names, stale, as they do until the user deletes them: a
# sentence read by the wrong block's names, or off the vocab note, shows
TWO_TYPE_CONFIG = {
    SENTENCE: {
        "vocab_note_type": VOCAB,
        "word_list_field": "s-array",
        "sentence_field": "s-sentence",
        "translated_sentence_field": "s-translation",
    },
    VOCAB: {
        "sentence_note_type": SENTENCE,
        "word_sort_field": "v-sort",
        "word_kanjified_field": "v-word",
        "word_reading_field": "v-reading",
        "translated_sentence_field": "v-translation",
        "example_sentence_id_field": "v-example-id",
    },
}


class TypedNote:
    def __init__(self, note_id, type_name, fields):
        self.id = note_id
        self.type_name = type_name
        self.fields = fields

    def note_type(self):
        return {"name": self.type_name}

    def __getitem__(self, key):
        return self.fields[key]

    def __contains__(self, key):
        return key in self.fields


def sentence_note(note_id, arr, translation):
    return TypedNote(
        note_id,
        SENTENCE,
        {
            "s-sentence": "猫と猫",
            "s-array": json.dumps(arr, ensure_ascii=False),
            "s-translation": translation,
        },
    )


def vocab_note(note_id, example_id="", old_array=()):
    return TypedNote(
        note_id,
        VOCAB,
        {
            "v-sort": "猫",
            "v-word": "猫",
            "v-reading": "ねこ",
            "v-translation": "the vocab note's copy",
            "v-example-id": str(example_id),
            "s-sentence": "古い文",
            "s-array": json.dumps(list(old_array), ensure_ascii=False),
            "s-translation": "the old translation",
        },
    )


class NotFound(Exception):
    """anki.errors.NotFoundError, which the suite's stubs make a class no code can raise."""


class TwoTypeCollection:
    """The collection behind clean_meaning's `col_find_notes` / `col_get_notes`. The search
    reads the terms the sentence searches are made of (a `"field:*text*"` term a substring, as
    in Anki), and a fetch of an id with no note raises, as Anki's get_note does."""

    def __init__(self, *notes: TypedNote) -> None:
        self.notes = {note.id: note for note in notes}
        self.searches: list[str] = []
        self.fetches: list[list[int]] = []

    def find_notes(self, query):
        self.searches.append(query)
        of_type = re.search(r'"note:([^"]+)"', query)
        terms = re.findall(r'"([^":]+):\*(\d+)\*"', query)
        left_out = {int(nid) for nid in re.findall(r"-nid:(\d+)", query)}
        return [
            nid
            for nid, note in self.notes.items()
            if (of_type is None or note.type_name == of_type.group(1))
            and nid not in left_out
            and all(field in note and text in note[field] for field, text in terms)
        ]

    def get_notes(self, note_ids):
        ids = list(note_ids)
        self.fetches.append(ids)
        missing = [nid for nid in ids if nid not in self.notes]
        if missing:
            raise NotFound(missing)
        return [self.notes[nid] for nid in ids]


# The vocab note's example links it at the second 猫, the other sentence note at the first
EXAMPLE_SENTENCE = {"jp_sentence": "猫と<b>猫</b>", "en_sentence": "cat and cat"}
OTHER_SENTENCE = {"jp_sentence": "<b>猫</b>と猫", "en_sentence": "the other cat"}
GIVEN_SENTENCE = {"jp_sentence": "ここの<b>猫</b>", "en_sentence": "the cat here"}


class TwoTypeSentencesTests(unittest.TestCase):
    """The two-type layout: a vocab note's own sentence is its example sentence note's, the
    others are the notes of its sentence type that link it, all read by the sentence type's
    field names and never off the vocab note."""

    def setUp(self):
        self.vocab = vocab_note(10, example_id=20, old_array=cat_array([10], [10]))
        self.col = TwoTypeCollection(
            sentence_note(20, cat_array([5], [10]), "cat and cat"),
            sentence_note(21, cat_array([10], ["match"]), "the other cat"),
            self.vocab,
            # A vocab note whose old array links the note: only an untyped search finds it
            vocab_note(11, example_id=21, old_array=cat_array([10], [11])),
        )
        for patch in (
            mock.patch.object(cm, "col_find_notes", self.col.find_notes),
            mock.patch.object(cm, "col_get_notes", self.col.get_notes),
            mock.patch.object(cm, "NotFoundError", NotFound),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def sentences(self, note, config=TWO_TYPE_CONFIG, **kwargs):
        return cm.get_sentences_for_note(config, note, **kwargs)

    def test_the_own_sentence_is_the_example_note_with_the_note_word_marked(self):
        self.assertEqual(self.sentences(self.vocab)[0], EXAMPLE_SENTENCE)

    def test_the_others_are_the_sentence_notes_linking_it_but_its_example(self):
        self.assertEqual(self.sentences(self.vocab), [EXAMPLE_SENTENCE, OTHER_SENTENCE])

    def test_the_search_names_the_sentence_type_and_leaves_the_example_out(self):
        self.sentences(self.vocab)
        self.assertEqual(self.col.searches, ['"note:Sentence" "s-array:*10*" -nid:20'])

    def test_excluded_the_own_sentence_is_not_even_read(self):
        self.assertEqual(self.sentences(self.vocab, exclude_self=True), [OTHER_SENTENCE])
        self.assertEqual(self.col.fetches, [[21]])

    def test_a_note_naming_no_example_has_only_the_others(self):
        # Every sentence note linking it is one of the others then, its example among them
        self.vocab.fields["v-example-id"] = ""
        with self.assertLogs(cm.logger, "WARNING"):
            sentences = self.sentences(self.vocab)
        self.assertEqual(sentences, [EXAMPLE_SENTENCE, OTHER_SENTENCE])
        self.assertEqual(self.col.searches, ['"note:Sentence" "s-array:*10*"'])

    def test_a_note_whose_example_is_gone_has_only_the_others(self):
        self.vocab.fields["v-example-id"] = "99"
        with self.assertLogs(cm.logger, "WARNING") as logs:
            sentences = self.sentences(self.vocab)
        self.assertIn("99", "\n".join(logs.output))
        self.assertEqual(sentences, [EXAMPLE_SENTENCE, OTHER_SENTENCE])
        self.assertEqual(self.col.searches, ['"note:Sentence" "s-array:*10*" -nid:99'])

    def test_the_example_is_read_through_the_run_note_cache(self):
        note_cache = cm.NoteCache()
        both = [EXAMPLE_SENTENCE, OTHER_SENTENCE]
        with mock.patch.object(note_cache_module, "get_notes", self.col.get_notes):
            # A sentence cache each time, so that the others are searched for again too
            for _ in range(2):
                sentence_cache = sc.SentenceCache()
                self.assertEqual(
                    self.sentences(
                        self.vocab, note_cache=note_cache, sentence_cache=sentence_cache
                    ),
                    both,
                )
            # Gone: the cache's fetch raises as the collection's does
            self.vocab.fields["v-example-id"] = "99"
            with self.assertLogs(cm.logger, "WARNING"):
                self.assertEqual(self.sentences(self.vocab, note_cache=note_cache), both)
        self.assertEqual(self.col.fetches, [[20], [21], [99]])

    def test_a_new_note_reads_its_example_and_searches_nothing(self):
        # Not added, so nothing links it: its word is marked where it first occurs
        new = vocab_note(0, example_id=20)
        self.assertEqual(
            self.sentences(new), [{"jp_sentence": "<b>猫</b>と猫", "en_sentence": "cat and cat"}]
        )
        self.assertEqual(self.col.searches, [])

    def test_a_new_note_naming_no_example_has_no_sentences(self):
        with self.assertLogs(cm.logger, "WARNING"):
            self.assertEqual(self.sentences(vocab_note(0)), [])
        self.assertEqual((self.col.searches, self.col.fetches), ([], []))

    def test_a_sentence_handed_in_is_a_new_note_own_and_nothing_is_read(self):
        with self.assertNoLogs(cm.logger, "WARNING"):
            sentences = self.sentences(vocab_note(0), own_sentence=GIVEN_SENTENCE)
        self.assertEqual(sentences, [GIVEN_SENTENCE])
        self.assertEqual((self.col.searches, self.col.fetches), ([], []))

    def test_a_sentence_handed_in_replaces_the_example_and_the_others_stay(self):
        sentences = self.sentences(self.vocab, own_sentence=GIVEN_SENTENCE)
        self.assertEqual(sentences, [GIVEN_SENTENCE, OTHER_SENTENCE])
        self.assertEqual(self.col.fetches, [[21]])

    def test_a_key_the_sentence_block_lacks_is_an_error_naming_it(self):
        config = copy.deepcopy(TWO_TYPE_CONFIG)
        del config[SENTENCE]["translated_sentence_field"]
        with self.assertRaises(Exception) as raised:
            self.sentences(self.vocab, config=config)
        self.assertNotIsInstance(raised.exception, KeyError)
        self.assertIn("translated_sentence_field", str(raised.exception))
        self.assertIn(SENTENCE, str(raised.exception))

    def test_a_broken_layout_is_a_layout_error(self):
        config = copy.deepcopy(TWO_TYPE_CONFIG)
        del config[SENTENCE]["vocab_note_type"]
        with self.assertRaises(note_roles.LayoutError):
            self.sentences(self.vocab, config=config)


if __name__ == "__main__":
    unittest.main()
