"""What the word index has to answer exactly as the collection searches it replaces did.

The index stands in for three Anki searches, and the one that matters most decides whether a
word already has a note. Miss a note the search would have found and the run creates a
duplicate in the user's collection, so the tests here are mostly about the edges where a
lookup could differ from a search: which notetypes a field search spans, how it treats case
and Unicode composition, what a negated field term does to notes that have no such field, and
what a note saved before a field existed looks like in `flds`.

Building it is tested separately from using it, because only the build needs an Anki: the
maps are built from plain rows, so everything above `_read_notes` is a pure function.
"""

import asyncio
import unittest

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path
import addon_modules  # noqa: F401
from anki_stubs import load_ops_module, mw

wi = load_ops_module("word_index")
# Kept because the cache tests replace it and have to put it back
REAL_BUILD = wi.build_word_index

FIELDS = wi.WordFields(
    kanjified="vocab-kanjified", normal="vocab", reading="vocab-kana", sort="vocab-key"
)

# The notetype the add-on is configured for: the four fields at the ordinals it really uses
VOCAB_MID = 1342706442510
VOCAB_ORDS = wi.FieldOrds(kanjified=12, normal=10, reading=4, sort=0)


def vocab_row(note_id: int, kanjified: str, normal: str, reading: str, sort: str) -> tuple:
    """A `select id, mid, flds` row for the vocab notetype, fields at their real ordinals."""
    values = [""] * 13
    values[0] = sort
    values[4] = reading
    values[10] = normal
    values[12] = kanjified
    return (note_id, VOCAB_MID, wi.FIELD_SEPARATOR.join(values))


def build(*rows, ords_by_mid=None) -> "wi.WordIndex":
    return wi.WordIndex.from_rows(FIELDS, ords_by_mid or {VOCAB_MID: VOCAB_ORDS}, list(rows))


class LookupTests(unittest.TestCase):
    def test_a_word_is_found_through_either_field(self):
        index = build(
            vocab_row(1, "私", "わたし", "わたし", "私"),
            vocab_row(2, "", "私", "わたし", "私 (r2)"),
        )
        self.assertEqual(index.matching_note_ids(["私"], ["私"]), [1, 2])

    def test_ids_come_back_in_id_order_without_duplicates(self):
        # A note carrying the word in both fields is one hit, and a search returned its hits
        # in table order, which is id order
        index = build(
            vocab_row(30, "私", "私", "わたし", "私"),
            vocab_row(10, "私", "私", "わたし", "私 (r2)"),
            vocab_row(20, "私", "私", "わたし", "私 (r3)"),
        )
        self.assertEqual(index.matching_note_ids(["私"], ["私"]), [10, 20, 30])

    def test_a_word_not_in_the_collection_finds_nothing(self):
        index = build(vocab_row(1, "私", "私", "わたし", "私"))
        self.assertEqual(index.matching_note_ids(["彼女"], ["彼女"]), [])

    def test_every_alternative_the_query_ored_together_is_looked_up(self):
        index = build(
            vocab_row(1, "勉強する", "勉強する", "べんきょうする", "勉強する"),
            vocab_row(2, "お茶", "お茶", "おちゃ", "お茶"),
        )
        self.assertEqual(index.matching_note_ids(["勉強", "勉強する"], ["勉強"]), [1])
        # The honorific spelled with kana, which the query adds for a 御-prefixed word
        self.assertEqual(index.matching_note_ids(["御茶", "お茶"], ["御茶", "お茶"]), [2])

    def test_only_note_id_narrows_to_that_note(self):
        index = build(
            vocab_row(1, "私", "私", "わたし", "私"),
            vocab_row(2, "私", "私", "わたし", "私 (r2)"),
        )
        self.assertEqual(index.matching_note_ids(["私"], ["私"], only_note_id=2), [2])
        # nid: for a note the word query did not hit intersects to nothing
        self.assertEqual(index.matching_note_ids(["私"], ["私"], only_note_id=9), [])

    def test_lookup_ignores_case_and_unicode_composition(self):
        # Anki compares field values under a collation that does both, so the index has to
        index = build(vocab_row(1, "Café", "CAFÉ", "かふぇ", "cafe"))
        self.assertEqual(index.matching_note_ids(["café"], ["café"]), [1])

    def test_an_empty_field_is_not_a_hit_for_the_empty_string(self):
        index = build(vocab_row(1, "", "私", "わたし", "私"))
        self.assertEqual(index.matching_note_ids([""], []), [])

    def test_a_note_short_of_the_ordinal_is_read_as_empty(self):
        # A note saved before a field was added to its notetype has fewer values in flds
        short = (1, VOCAB_MID, wi.FIELD_SEPARATOR.join(["私", "", "", "", "わたし"]))
        index = build(short)
        self.assertEqual(index.matching_note_ids(["私"], ["私"]), [])
        self.assertEqual(index.reading(1), "わたし")


class ReadingTests(unittest.TestCase):
    def test_the_reading_comes_back_for_the_filter_to_use(self):
        index = build(vocab_row(1, "私", "私", "わたし", "私"))
        self.assertEqual(index.reading(1), "わたし")

    def test_a_notetype_without_a_reading_field_answers_none(self):
        # The case the matching code used to write as `word_reading_field not in note`: such a
        # note cannot be matched by reading at all, which is different from an empty reading
        ords = wi.FieldOrds(kanjified=12, normal=10, reading=None, sort=0)
        index = build(vocab_row(1, "私", "私", "わたし", "私"), ords_by_mid={VOCAB_MID: ords})
        self.assertIsNone(index.reading(1))

    def test_an_empty_reading_is_an_empty_string_not_none(self):
        index = build(vocab_row(1, "私", "私", "", "私"))
        self.assertEqual(index.reading(1), "")


class ExclusionTests(unittest.TestCase):
    def test_a_note_marked_x_is_dropped(self):
        index = build(
            vocab_row(1, "私", "私", "わたし", "私"),
            vocab_row(2, "私", "私", "わたし", "私 (x1)"),
        )
        self.assertEqual(index.matching_note_ids(["私"], ["私"]), [1])

    def test_the_x_marker_is_matched_case_insensitively(self):
        # Anki's re: is case-insensitive unless told otherwise
        index = build(vocab_row(1, "私", "私", "わたし", "私 (X1)"))
        self.assertEqual(index.matching_note_ids(["私"], ["私"]), [])

    def test_the_marker_is_found_anywhere_in_the_sort_field(self):
        index = build(vocab_row(1, "私", "私", "わたし", "私 (kun)(x2)(m1)"))
        self.assertTrue(index.is_excluded(1))

    def test_a_notetype_without_a_sort_field_is_not_excluded(self):
        # The term is a negated field search, and a field search only matches notes that have
        # the field - so its negation keeps everything that does not
        ords = wi.FieldOrds(kanjified=12, normal=10, reading=4, sort=None)
        index = build(vocab_row(1, "私", "私", "わたし", "私 (x1)"), ords_by_mid={VOCAB_MID: ords})
        self.assertFalse(index.is_excluded(1))
        self.assertEqual(index.matching_note_ids(["私"], ["私"]), [1])


class SeveralNotetypesTests(unittest.TestCase):
    """A field search matches any notetype that has a field of that name, so the index must."""

    OTHER_MID = 999
    # A second notetype sharing only the plain word field, at a different ordinal
    OTHER_ORDS = wi.FieldOrds(kanjified=None, normal=1, reading=None, sort=None)

    def other_row(self, note_id: int, normal: str) -> tuple:
        return (note_id, self.OTHER_MID, wi.FIELD_SEPARATOR.join(["front", normal]))

    def test_a_note_of_another_notetype_with_the_field_is_found(self):
        index = wi.WordIndex.from_rows(
            FIELDS,
            {VOCAB_MID: VOCAB_ORDS, self.OTHER_MID: self.OTHER_ORDS},
            [vocab_row(1, "私", "私", "わたし", "私"), self.other_row(2, "私")],
        )
        self.assertEqual(index.matching_note_ids(["私"], ["私"]), [1, 2])
        # ...but it has no reading, so the reading filter drops it, exactly as the old code's
        # `word_reading_field in note` check did
        self.assertIsNone(index.reading(2))

    def test_a_row_of_an_unindexed_notetype_is_skipped(self):
        index = wi.WordIndex.from_rows(
            FIELDS, {VOCAB_MID: VOCAB_ORDS}, [self.other_row(2, "私")]
        )
        self.assertEqual(index.matching_note_ids(["私"], ["私"]), [])


class MarkerQueryTests(unittest.TestCase):
    """The (kun)/(on)/(rN)/(mN) query create_new_note_without_matching asks per new note."""

    REGEX = r"^{word} ?(?:\((?:kun|on)\))?(?:\(r\d+\))?(?:\(m\d+\))?$"

    def marker_ids(self, index, word):
        return index.marker_note_ids(word, self.REGEX.format(word=word))

    def test_the_bare_word_and_every_marker_combination_match(self):
        index = build(
            vocab_row(1, "私", "私", "わたし", "私"),
            vocab_row(2, "私", "私", "わたくし", "私 (kun)"),
            vocab_row(3, "私", "私", "わたし", "私 (on)(r2)"),
            vocab_row(4, "私", "私", "わたし", "私 (r3)(m2)"),
        )
        self.assertEqual(self.marker_ids(index, "私"), [1, 2, 3, 4])

    def test_a_different_base_word_does_not_match(self):
        index = build(
            vocab_row(1, "私達", "私達", "わたしたち", "私達"),
            vocab_row(2, "私", "私", "わたし", "私"),
        )
        self.assertEqual(self.marker_ids(index, "私"), [2])

    def test_a_marker_the_regex_does_not_allow_does_not_match(self):
        index = build(
            vocab_row(1, "私", "私", "わたし", "私 (x1)"),
            vocab_row(2, "私", "私", "わたし", "私 (kun)"),
        )
        self.assertEqual(self.marker_ids(index, "私"), [2])

    def test_a_word_holding_a_regex_metacharacter_still_matches_by_the_regex(self):
        # The word goes into the regex unescaped, so the bucket it would be filed under is not
        # necessarily the bucket the regex can match - the index falls back to scanning
        index = build(
            vocab_row(1, "A.C", "A.C", "えーしーだ", "A.C"),
            vocab_row(2, "ABC", "ABC", "えーびーしー", "ABC (r2)"),
            vocab_row(3, "XYZ", "XYZ", "えっくす", "XYZ"),
        )
        self.assertEqual(self.marker_ids(index, "A.C"), [1, 2])

    def test_the_bucket_lookup_ignores_case(self):
        index = build(vocab_row(1, "abc", "abc", "えー", "ABC (kun)"))
        self.assertEqual(self.marker_ids(index, "abc"), [1])


class ReadNotesTests(unittest.TestCase):
    """The one collection turn: which notetypes get scanned, and what is asked of the db."""

    class FakeModels:
        def __init__(self, notetypes):
            self._notetypes = notetypes

        def all(self):
            return self._notetypes

    class FakeDb:
        def __init__(self, rows):
            self.rows = rows
            self.queries = []

        def all(self, sql, *args):
            self.queries.append(sql)
            return self.rows

    class FakeCollection:
        def __init__(self, notetypes, rows):
            self.models = ReadNotesTests.FakeModels(notetypes)
            self.db = ReadNotesTests.FakeDb(rows)

    @staticmethod
    def notetype(mid, *field_names):
        return {
            "id": mid,
            "flds": [{"name": name, "ord": i} for i, name in enumerate(field_names)],
        }

    def read(self, notetypes, rows=()):
        mw.col = self.FakeCollection(notetypes, list(rows))
        return wi._read_notes(FIELDS), mw.col

    def test_a_notetype_with_none_of_the_searched_fields_is_left_out(self):
        (ords_by_mid, _), col = self.read(
            [
                self.notetype(VOCAB_MID, "vocab-key", "vocab", "vocab-kana", "vocab-kanjified"),
                self.notetype(500, "Front", "Back"),
            ]
        )
        self.assertEqual(list(ords_by_mid), [VOCAB_MID])
        self.assertIn(str(VOCAB_MID), col.db.queries[0])
        self.assertNotIn("500", col.db.queries[0])

    def test_a_notetype_with_only_the_sort_field_is_kept(self):
        # It cannot be hit by a word query, but the marker query searches the sort field alone
        (ords_by_mid, _), _ = self.read(
            [self.notetype(600, "vocab-key", "Back")]
        )
        self.assertEqual(
            ords_by_mid[600], wi.FieldOrds(kanjified=None, normal=None, reading=None, sort=0)
        )

    def test_a_notetype_with_only_the_reading_field_is_left_out(self):
        # Nothing searches the reading field, so such a note can never be a hit
        (ords_by_mid, _), _ = self.read([self.notetype(700, "vocab-kana", "Back")])
        self.assertEqual(ords_by_mid, {})

    def test_ordinals_come_from_each_notetype_rather_than_being_assumed(self):
        (ords_by_mid, _), _ = self.read(
            [self.notetype(VOCAB_MID, "vocab", "vocab-kana", "vocab-kanjified", "vocab-key")]
        )
        self.assertEqual(
            ords_by_mid[VOCAB_MID],
            wi.FieldOrds(kanjified=2, normal=0, reading=1, sort=3),
        )

    def test_nothing_is_asked_of_the_db_when_no_notetype_qualifies(self):
        (ords_by_mid, rows), col = self.read([self.notetype(500, "Front", "Back")])
        self.assertEqual((ords_by_mid, rows), ({}, []))
        self.assertEqual(col.db.queries, [])

    def test_the_whole_scan_is_one_query(self):
        rows = [vocab_row(1, "私", "私", "わたし", "私")]
        (_, read_rows), col = self.read(
            [self.notetype(VOCAB_MID, "vocab-key", "vocab", "vocab-kana", "vocab-kanjified")],
            rows,
        )
        self.assertEqual(read_rows, rows)
        self.assertEqual(len(col.db.queries), 1)


class CacheTests(unittest.TestCase):
    """Every task wants the same index, and the run can only afford to build it once."""

    def setUp(self):
        self.builds = []

    def counting_build(self, delay: float = 0.0):
        async def build(fields):
            self.builds.append(fields)
            await asyncio.sleep(delay)
            return wi.WordIndex(fields)

        return build

    def test_tasks_arriving_together_share_one_build(self):
        cache = wi.WordIndexCache()
        wi.build_word_index = self.counting_build(delay=0.01)

        async def race():
            return await asyncio.gather(*(cache.get(FIELDS) for _ in range(20)))

        indexes = asyncio.run(race())
        self.assertEqual(len(self.builds), 1)
        # And they all got the same object, not one each
        self.assertEqual(len({id(index) for index in indexes}), 1)

    def test_a_later_task_reuses_the_finished_index(self):
        cache = wi.WordIndexCache()
        wi.build_word_index = self.counting_build()

        async def twice():
            first = await cache.get(FIELDS)
            return first, await cache.get(FIELDS)

        first, second = asyncio.run(twice())
        self.assertEqual(len(self.builds), 1)
        self.assertIs(first, second)

    def test_a_different_field_set_gets_its_own_index(self):
        cache = wi.WordIndexCache()
        wi.build_word_index = self.counting_build()
        other = wi.WordFields(kanjified="k2", normal="n2", reading="r2", sort="s2")

        async def both():
            return await cache.get(FIELDS), await cache.get(other)

        first, second = asyncio.run(both())
        self.assertEqual(self.builds, [FIELDS, other])
        self.assertIsNot(first, second)

    def test_a_failed_build_is_not_cached(self):
        cache = wi.WordIndexCache()
        attempts = []

        async def build(fields):
            attempts.append(fields)
            if len(attempts) == 1:
                raise RuntimeError("the run was cancelled")
            return wi.WordIndex(fields)

        wi.build_word_index = build

        async def retry():
            with self.assertRaises(RuntimeError):
                await cache.get(FIELDS)
            return await cache.get(FIELDS)

        self.assertIsNotNone(asyncio.run(retry()))
        self.assertEqual(len(attempts), 2)

    def tearDown(self):
        # The module attribute is what the cache calls, so put the real one back
        wi.build_word_index = REAL_BUILD


if __name__ == "__main__":
    unittest.main()
