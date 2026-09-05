"""The two dictionary lookups that were scanning a table their own index already covers.

Nothing in the suite reached `mdx_dictionary.py` before this, because it imports aqt and
lives outside `async_api_ops`. `anki_stubs.load_ops_module` takes a subdir for exactly that.

Everything here runs against a temporary SQLite database shaped like a real `.mdx.db` - the
`MDX_INDEX` table and the `key_index` that `IndexBuilder` puts on it - with a stub standing in
for the builder. That is the whole of what the two changes touch, and it means the tests say
something about SQLite's actual behaviour rather than about a fake of it.

The prefix rewrite is the part that needs holding: `LIKE 'p%'` and a `>= ? AND < ?` range are
the same question only for prefixes with no ASCII letter and no wildcard character, and none
of the three ways they can differ occurs in the kana readings this add-on looks up. So the
workload cannot tell anyone if a rewrite gets it wrong; only these can.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import addon_modules  # noqa: F401  (puts the vendored lib/ on sys.path)
import anki_stubs

mdx = anki_stubs.load_ops_module("mdx_dictionary", "sync_local_ops")


# Keys in the order a real .mdx.db holds them: MDX files carry their keys sorted, and the
# index build inserts them in that order, which is why a scan and an index walk agree.
KEYS = [
    "Apple",
    "apple",
    "applesauce",
    "ごはん",
    "ご飯",
    "たべる",
    "た_べ",
    "た%び",
    "食べる",
    "食べ物",
]


class StubBuilder:
    """The one attribute of IndexBuilder that the two changes use, plus the shipped LIKE."""

    def __init__(self, db_path: str):
        self._mdx_db = db_path
        self._title = "stub"
        self._description = ""

    def get_mdx_keys(self, query: str = "") -> list:
        """`IndexBuilder.get_keys`, copied so a test can compare against what shipped."""
        if query:
            query = query.replace("*", "%") if "*" in query else query + "%"
            sql = 'SELECT key_text FROM MDX_INDEX WHERE key_text LIKE "' + query + '"'
        else:
            sql = "SELECT key_text FROM MDX_INDEX"
        conn = sqlite3.connect(self._mdx_db)
        try:
            return [row[0] for row in conn.execute(sql)]
        finally:
            conn.close()


class MDXDictionaryTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.dir.name) / "stub.mdx.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE MDX_INDEX (key_text text not null unique, file_pos integer,"
            " compressed_size integer, decompressed_size integer, record_block_type integer,"
            " record_start integer, record_end integer, offset integer)"
        )
        conn.executemany(
            "INSERT INTO MDX_INDEX VALUES (?, 0, 0, 0, 0, 0, 0, 0)",
            [(key,) for key in KEYS],
        )
        conn.execute("CREATE INDEX key_index ON MDX_INDEX (key_text)")
        conn.commit()
        conn.close()
        self.dictionary = self.make_dictionary()

    def tearDown(self):
        self.dir.cleanup()

    def make_dictionary(self):
        """An MDXDictionary over the stub database, without loading a real .mdx file."""
        dictionary = object.__new__(mdx.MDXDictionary)
        dictionary.mdx_path = str(Path(self.dir.name) / "stub.mdx")
        dictionary.builder = StubBuilder(self.db_path)
        return dictionary

    def shipped_prefix_keys(self, prefix: str, max_results: int = 10) -> list:
        """What `get_keys_by_prefix` returned before the rewrite."""
        keys = self.dictionary.builder.get_mdx_keys(f"{prefix}*")
        return keys[:max_results] if keys else []

    def indexes_on(self, db_path=None) -> list:
        conn = sqlite3.connect(db_path or self.db_path)
        try:
            # (seq, name, unique, origin, partial)
            return [row[1] for row in conn.execute("PRAGMA index_list(MDX_INDEX)")]
        finally:
            conn.close()


class PrefixRangeTests(MDXDictionaryTestCase):
    """`LIKE 'p%'` will not use a BINARY index; the same question as a range will."""

    def test_a_kana_prefix_returns_what_the_like_pattern_returned(self):
        for prefix in ("ご", "た", "食べ", "食"):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    self.dictionary.get_keys_by_prefix(prefix),
                    self.shipped_prefix_keys(prefix),
                )

    def test_the_order_is_the_same_and_so_the_truncation_is_too(self):
        """The result is cut to max_results and the caller filters what survives.

        Set equality is not enough here: if the range walked the keys in a different order
        from the scan, the ten that survive would be a different ten and the definitions built
        from them would differ.
        """
        self.assertEqual(
            self.dictionary.get_keys_by_prefix("た", max_results=2),
            self.shipped_prefix_keys("た", max_results=2),
        )

    def test_a_prefix_that_matches_nothing_still_matches_nothing(self):
        self.assertEqual(self.dictionary.get_keys_by_prefix("ざ"), [])

    def test_an_ascii_prefix_keeps_the_case_folding_like_does(self):
        """LIKE is case-insensitive for ASCII and a range is not, so ASCII falls back.

        Losing this would silently drop "Apple" from a lookup for "apple".
        """
        self.assertEqual(
            self.dictionary.get_keys_by_prefix("apple"),
            self.shipped_prefix_keys("apple"),
        )
        self.assertIn("Apple", self.dictionary.get_keys_by_prefix("apple"))

    def test_a_wildcard_in_the_prefix_stays_a_wildcard(self):
        """`%`, `_` and `*` are wildcards to the pattern and literals to a range."""
        for prefix in ("た_", "た%", "た*"):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    self.dictionary.get_keys_by_prefix(prefix),
                    self.shipped_prefix_keys(prefix),
                )

    def test_an_empty_prefix_falls_back(self):
        self.assertEqual(
            self.dictionary.get_keys_by_prefix(""),
            self.shipped_prefix_keys(""),
        )

    def test_the_range_actually_seeks(self):
        """The whole point: the plan has to be a search, not a scan.

        Without this the rewrite could be correct and worthless - which is precisely what the
        shipped LIKE is, against an index that has been sitting there the whole time.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            plan = " ".join(
                str(row)
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT key_text FROM MDX_INDEX"
                    " WHERE key_text >= ? AND key_text < ? LIMIT ?",
                    ("た", "だ", 10),
                )
            )
        finally:
            conn.close()
        self.assertIn("key_index", plan)
        self.assertNotIn("SCAN", plan)

    def test_the_last_code_point_there_is_falls_back_rather_than_overflowing(self):
        # chr(ord(c) + 1) has nowhere to go above U+10FFFF
        self.assertIsNone(self.dictionary._keys_in_prefix_range("\U0010ffff", 10))

    def test_a_missing_database_is_not_an_error(self):
        self.dictionary.builder._mdx_db = str(Path(self.dir.name) / "gone.mdx.db")
        self.assertEqual(self.dictionary.get_keys_by_prefix("た"), [])


class LowerKeyIndexTests(MDXDictionaryTestCase):
    """`lower(key_text) = lower(?)` cannot use `key_index`; an expression index it can."""

    def test_the_index_is_created(self):
        self.assertNotIn(mdx.LOWER_KEY_INDEX, self.indexes_on())
        self.dictionary._add_lower_key_index()
        self.assertIn(mdx.LOWER_KEY_INDEX, self.indexes_on())

    def test_the_shipped_case_insensitive_lookup_seeks_once_it_exists(self):
        """No SQL changes anywhere: the vendored query becomes a seek on its own."""
        self.dictionary._add_lower_key_index()
        conn = sqlite3.connect(self.db_path)
        try:
            plan = " ".join(
                str(row)
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM MDX_INDEX"
                    ' WHERE lower(key_text) = lower("apple")'
                )
            )
        finally:
            conn.close()
        self.assertIn(mdx.LOWER_KEY_INDEX, plan)
        self.assertNotIn("SCAN", plan)

    def test_the_same_rows_come_back_with_the_index_as_without(self):
        def rows():
            conn = sqlite3.connect(self.db_path)
            try:
                return sorted(
                    row[0]
                    for row in conn.execute(
                        "SELECT key_text FROM MDX_INDEX WHERE lower(key_text) = lower(?)",
                        ("APPLE",),
                    )
                )
            finally:
                conn.close()

        before = rows()
        self.dictionary._add_lower_key_index()
        self.assertEqual(rows(), before)
        self.assertEqual(before, ["Apple", "apple"])

    def test_creating_it_twice_is_free_and_harmless(self):
        self.dictionary._add_lower_key_index()
        self.dictionary._add_lower_key_index()
        self.assertEqual(self.indexes_on().count(mdx.LOWER_KEY_INDEX), 1)

    def test_a_database_that_cannot_be_written_degrades_to_the_old_behaviour(self):
        """A read-only dictionary directory or a full disk must not fail a load.

        The lookups all still work without the index; they just scan the way they always did.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA query_only = ON")
        finally:
            conn.close()
        Path(self.db_path).chmod(0o444)
        try:
            self.dictionary._add_lower_key_index()  # must not raise
        finally:
            Path(self.db_path).chmod(0o644)

    def test_a_missing_database_is_not_an_error(self):
        self.dictionary.builder._mdx_db = str(Path(self.dir.name) / "gone.mdx.db")
        self.dictionary._add_lower_key_index()  # must not raise


if __name__ == "__main__":
    unittest.main()
