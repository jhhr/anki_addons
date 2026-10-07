"""The run's note cache leaves out an id the collection has no note for, as its callers expect.

Anki's `get_note` raises `NotFoundError` for such an id, and a batch fetched in one turn raises
with it, which the cache passed through while saying it did not: a match run rating a word
linked to a note deleted since then failed there instead of leaving the word unrated.
"""

import asyncio
import types
import unittest
from unittest import mock

from addon_modules import load_ops_module

nc = load_ops_module("note_cache")
mwtn = load_ops_module("match_words_to_notes")


class NotFound(Exception):
    """anki.errors.NotFoundError, which the suite's stubs make a class no code can raise."""


class FakeNote:
    def __init__(self, note_id: int, fields: "dict[str, str] | None" = None) -> None:
        self.id = note_id
        self.fields = fields or {}

    def __contains__(self, key: str) -> bool:
        return key in self.fields

    def __getitem__(self, key: str) -> str:
        return self.fields[key]


class Collection:
    """The fetch seam of note_cache: a batch holding an id with no note raises, as get_note
    does for that id, and fails the whole batch with it."""

    def __init__(self, *notes: FakeNote) -> None:
        self.notes = {note.id: note for note in notes}
        self.fetches: "list[list[int]]" = []

    def get_notes(self, ids) -> list:
        ids = list(ids)
        self.fetches.append(ids)
        if any(nid not in self.notes for nid in ids):
            raise NotFound(ids)
        return [self.notes[nid] for nid in ids]

    async def get_notes_async(self, ids) -> list:
        return self.get_notes(ids)


class CacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.collection = Collection(FakeNote(1), FakeNote(2))
        for patch in (
            mock.patch.object(nc, "NotFoundError", NotFound),
            mock.patch.object(nc, "get_notes", self.collection.get_notes),
            mock.patch.object(nc, "get_notes_async", self.collection.get_notes_async),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.cache = nc.NoteCache()


class GoneNoteTests(CacheTestCase):
    def test_a_gone_note_is_left_out_and_the_rest_fetched(self):
        found = asyncio.run(self.cache.get_notes([1, 99, 2]))

        self.assertEqual(sorted(found), [1, 2])
        # The batch, then each alone once it raised
        self.assertEqual(self.collection.fetches, [[1, 99, 2], [1], [99], [2]])

    def test_the_blocking_fetch_leaves_it_out_the_same(self):
        found = self.cache.get_notes_blocking([1, 99, 2])

        self.assertEqual(sorted(found), [1, 2])
        self.assertEqual(self.collection.fetches, [[1, 99, 2], [1], [99], [2]])

    def test_the_notes_found_are_cached_and_the_gone_one_asked_for_again(self):
        asyncio.run(self.cache.get_notes([1, 99]))
        self.collection.fetches.clear()

        found = asyncio.run(self.cache.get_notes([1, 99]))

        self.assertEqual(list(found), [1])
        self.assertEqual(self.collection.fetches, [[99]])

    def test_a_batch_of_existing_notes_is_still_one_fetch(self):
        found = asyncio.run(self.cache.get_notes([1, 2]))

        self.assertEqual(sorted(found), [1, 2])
        self.assertEqual(self.collection.fetches, [[1, 2]])


class RatingTests(CacheTestCase):
    """rate_linked_word over the run's real note cache."""

    def rate(self, linked_id: int):
        def get_response(*_, **__):
            raise AssertionError("A word linked to a gone note is not asked about")

        target = types.SimpleNamespace(
            elem=["借りた", "verb", "借りる", "かりる", [linked_id], []],
            word="借りる",
            reading="かりる",
            part_of_speech="verb",
        )
        fields = {key: key for key in mwtn.MATCH_FIELD_KEYS}
        with mock.patch.object(mwtn, "get_response", get_response):
            return asyncio.run(
                mwtn.rate_linked_word(
                    config={"match_words_model": "model"},
                    target=target,
                    prompt_sentence="本を<b>借りた</b>。",
                    fields=fields,
                    notes_to_update_dict={},
                    note_cache=self.cache,
                    cancel_state=None,
                    log_prefix="",
                )
            )

    def test_a_word_linked_to_a_deleted_note_is_left_unrated(self):
        self.assertIsNone(self.rate(99))
        self.assertEqual(self.collection.fetches, [[99]])


if __name__ == "__main__":
    unittest.main()
