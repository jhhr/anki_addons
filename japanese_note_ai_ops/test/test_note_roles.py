"""Which configured note type plays which role, and what a vocab note copies from its example.

Two layouts must both hold: the one-type layout, where one block has both roles and every
helper answers as the code did before sentence notes (every fixture and test of the ops is
one-type), and the two-type layout, a sentence type and a vocab type naming each other. A config
that is neither must fail with a message naming both types, never be read halfway.
"""

import unittest

from addon_modules import load_addon_module

roles = load_addon_module("note_roles", subdir="")

# Hardcoded by the addon's add hook, so not a name of anyone's collection
VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"


def one_type_config() -> dict:
    return {
        "log_level": "ERROR",
        VOCAB: {
            "sentence_field": "Sentence",
            "furigana_sentence_field": "Sentence furigana",
            "kanjified_sentence_field": "Sentence kanjified",
            "word_extraction_sentence_field": "Sentence extraction",
            "translated_sentence_field": "Sentence translation",
            "sentence_audio_field": "Sentence audio",
            "word_list_field": "Words",
            "word_kanjified_field": "Word kanjified",
            "word_normal_field": "Word",
            "word_reading_field": "Word reading",
            "word_sort_field": "Word sort",
            "meaning_field": "Meaning",
            "new_note_id_field": "Note id",
            "insert_deck": "Japanese",
        },
        "Kanji draw": {"kanji_field": "Kanji", "story_field": "Story"},
    }


def two_type_config() -> dict:
    # The vocab type's copies have names of their own, so a pair read the wrong way round fails
    return {
        "log_level": "ERROR",
        SENTENCE: {
            "vocab_note_type": VOCAB,
            "word_list_field": "Words",
            "sentence_field": "Text",
            "furigana_sentence_field": "Text furigana",
            "kanjified_sentence_field": "Text kanjified",
            "word_extraction_sentence_field": "Text extraction",
            "translated_sentence_field": "Text translation",
            "sentence_audio_field": "Text audio",
            "sentence_seen_count_field": "Seen count",
            "insert_deck": "Sentences",
        },
        VOCAB: {
            "sentence_note_type": SENTENCE,
            "example_sentence_id_field": "Example id",
            "translated_sentence_field": "Example translation",
            "sentence_audio_field": "Example audio",
            "word_kanjified_field": "Word kanjified",
            "word_normal_field": "Word",
            "word_reading_field": "Word reading",
            "word_sort_field": "Word sort",
            "meaning_field": "Meaning",
            "new_note_id_field": "Note id",
            "insert_deck": "Japanese",
        },
        "Kanji draw": {"kanji_field": "Kanji", "story_field": "Story"},
    }


def sentence_note() -> dict:
    return {
        "Words": '[["A"]]',
        "Text": "text",
        "Text furigana": "text furigana",
        "Text kanjified": "text kanjified",
        "Text extraction": "text extraction",
        "Text translation": "text translation",
        "Text audio": "[sound:text.mp3]",
        "Seen count": "7",
    }


def vocab_note() -> dict:
    return {
        "Example id": "",
        "Example translation": "old translation",
        "Example audio": "",
        "Word kanjified": "word",
        "Word": "word",
        "Word reading": "reading",
        "Word sort": "word",
        "Meaning": "meaning",
        "Note id": "-5",
    }


def one_type_note(prefix: str) -> dict:
    fields = [
        "Sentence",
        "Sentence furigana",
        "Sentence kanjified",
        "Sentence extraction",
        "Sentence translation",
        "Sentence audio",
        "Words",
        "Word kanjified",
        "Word",
        "Word reading",
        "Word sort",
        "Meaning",
        "Note id",
    ]
    return {field: f"{prefix} {field}" for field in fields}


class RoleTests(unittest.TestCase):
    def test_a_one_type_block_has_both_roles(self):
        config = one_type_config()
        self.assertTrue(roles.is_sentence_type(config, VOCAB))
        self.assertTrue(roles.is_vocab_type(config, VOCAB))

    def test_each_block_of_the_two_type_layout_has_one_role(self):
        config = two_type_config()
        self.assertTrue(roles.is_sentence_type(config, SENTENCE))
        self.assertFalse(roles.is_vocab_type(config, SENTENCE))
        self.assertTrue(roles.is_vocab_type(config, VOCAB))
        self.assertFalse(roles.is_sentence_type(config, VOCAB))

    def test_a_type_without_the_keys_has_no_role(self):
        config = one_type_config()
        # Configured for other ops, not configured at all, or a plain setting of the top level
        for name in ["Kanji draw", "Basic", "log_level"]:
            with self.subTest(name):
                self.assertFalse(roles.is_sentence_type(config, name))
                self.assertFalse(roles.is_vocab_type(config, name))

    def test_an_empty_key_gives_no_role(self):
        config = one_type_config()
        config[VOCAB]["word_list_field"] = ""
        config[VOCAB]["word_sort_field"] = ""
        self.assertFalse(roles.is_sentence_type(config, VOCAB))
        self.assertFalse(roles.is_vocab_type(config, VOCAB))


class OneTypeLayoutTests(unittest.TestCase):
    def test_the_type_is_its_own_vocab_and_sentence_type(self):
        config = one_type_config()
        self.assertEqual(roles.vocab_type_of(config, VOCAB), VOCAB)
        self.assertEqual(roles.sentence_type_of(config, VOCAB), VOCAB)

    def test_the_layout_is_usable(self):
        config = one_type_config()
        self.assertIsNone(roles.layout_error(config, VOCAB))
        self.assertIsNone(roles.layout_error(config, "Kanji draw"))
        self.assertEqual(roles.vocab_type_of(config, "Kanji draw"), "Kanji draw")

    def test_a_block_naming_itself_is_the_one_type_layout(self):
        config = one_type_config()
        config[VOCAB]["vocab_note_type"] = VOCAB
        config[VOCAB]["sentence_note_type"] = VOCAB
        self.assertIsNone(roles.layout_error(config, VOCAB))
        self.assertEqual(roles.vocab_type_of(config, VOCAB), VOCAB)
        self.assertEqual(roles.sentence_type_of(config, VOCAB), VOCAB)

    def test_there_is_no_example_id_field(self):
        self.assertIsNone(roles.example_id_field(one_type_config(), VOCAB))


class TwoTypeLayoutTests(unittest.TestCase):
    def test_each_type_names_the_other(self):
        config = two_type_config()
        self.assertEqual(roles.vocab_type_of(config, SENTENCE), VOCAB)
        self.assertEqual(roles.sentence_type_of(config, VOCAB), SENTENCE)

    def test_the_layout_is_usable_from_either_side(self):
        config = two_type_config()
        self.assertIsNone(roles.layout_error(config, SENTENCE))
        self.assertIsNone(roles.layout_error(config, VOCAB))
        self.assertIsNone(roles.layout_error(config, "Kanji draw"))

    def test_the_example_id_field_is_the_vocab_blocks(self):
        self.assertEqual(roles.example_id_field(two_type_config(), VOCAB), "Example id")


class InconsistentLayoutTests(unittest.TestCase):
    def assert_refused(self, config, note_type_name, *names):
        """The pre-flight's message names every type given, and vocab_type_of and
        sentence_type_of raise it rather than answer from half a layout."""
        error = roles.layout_error(config, note_type_name)
        self.assertIsNotNone(error)
        for name in names:
            self.assertIn(f'"{name}"', error)
        for derive in (roles.vocab_type_of, roles.sentence_type_of):
            with self.assertRaises(roles.LayoutError) as raised:
                derive(config, note_type_name)
            self.assertEqual(str(raised.exception), error)
        return error

    def test_a_vocab_type_not_naming_the_sentence_type_back(self):
        config = two_type_config()
        del config[VOCAB]["sentence_note_type"]
        error = self.assert_refused(config, SENTENCE, SENTENCE, VOCAB)
        self.assertIn("has no sentence_note_type", error)
        # Asked from the other side, the claim on it is found all the same
        self.assertEqual(self.assert_refused(config, VOCAB, SENTENCE, VOCAB), error)

    def test_a_sentence_type_not_naming_the_vocab_type_back(self):
        config = two_type_config()
        del config[SENTENCE]["vocab_note_type"]
        error = self.assert_refused(config, VOCAB, SENTENCE, VOCAB)
        self.assertIn("has no vocab_note_type", error)
        self.assert_refused(config, SENTENCE, SENTENCE, VOCAB)

    def test_a_vocab_type_naming_another_sentence_type(self):
        config = two_type_config()
        config[VOCAB]["sentence_note_type"] = "Other sentences"
        self.assert_refused(config, SENTENCE, SENTENCE, VOCAB, "Other sentences")

    def test_a_second_sentence_type_claiming_the_vocab_type(self):
        config = two_type_config()
        config["Other sentences"] = dict(config[SENTENCE])
        self.assert_refused(config, "Other sentences", "Other sentences", VOCAB, SENTENCE)
        self.assert_refused(config, VOCAB, "Other sentences", VOCAB, SENTENCE)
        # The pair the vocab type names back is consistent in itself
        self.assertIsNone(roles.layout_error(config, SENTENCE))

    def test_a_named_type_that_is_not_configured(self):
        for kept, removed in [(SENTENCE, VOCAB), (VOCAB, SENTENCE)]:
            with self.subTest(removed=removed):
                config = two_type_config()
                del config[removed]
                error = self.assert_refused(config, kept, SENTENCE, VOCAB)
                self.assertIn(f'"{removed}" has not been configured', error)

    def test_a_type_naming_both_keys(self):
        config = two_type_config()
        config[SENTENCE]["sentence_note_type"] = VOCAB
        self.assert_refused(config, SENTENCE, SENTENCE, VOCAB)
        # The vocab type's own pair has that block as its partner
        self.assert_refused(config, VOCAB, SENTENCE)

    def test_each_type_keeps_to_its_role(self):
        changes = [
            (SENTENCE, "word_list_field", None),
            (SENTENCE, "word_sort_field", "Word sort"),
            (VOCAB, "word_sort_field", None),
            (VOCAB, "word_list_field", "Words"),
        ]
        for block, key, value in changes:
            with self.subTest(block=block, key=key):
                config = two_type_config()
                if value is None:
                    del config[block][key]
                else:
                    config[block][key] = value
                for asked in (SENTENCE, VOCAB):
                    error = self.assert_refused(config, asked, SENTENCE, VOCAB)
                    self.assertIn(key, error)

    def test_a_type_not_configured_at_all(self):
        config = one_type_config()
        self.assertEqual(
            roles.layout_error(config, "Basic"),
            'Note type "Basic" has not been configured in the settings.',
        )
        with self.assertRaises(roles.LayoutError):
            roles.vocab_type_of(config, "Basic")


class ExampleFieldPairsTests(unittest.TestCase):
    def test_one_type_maps_every_sentence_field_onto_itself(self):
        self.assertEqual(
            roles.example_field_pairs(one_type_config(), VOCAB),
            [
                ("Sentence", "Sentence"),
                ("Sentence furigana", "Sentence furigana"),
                ("Sentence kanjified", "Sentence kanjified"),
                ("Sentence extraction", "Sentence extraction"),
                ("Sentence translation", "Sentence translation"),
                ("Sentence audio", "Sentence audio"),
            ],
        )

    def test_one_type_leaves_out_a_sentence_key_not_configured(self):
        config = one_type_config()
        del config[VOCAB]["translated_sentence_field"]
        config[VOCAB]["word_extraction_sentence_field"] = ""
        self.assertEqual(
            roles.example_field_pairs(config, VOCAB),
            [
                ("Sentence", "Sentence"),
                ("Sentence furigana", "Sentence furigana"),
                ("Sentence kanjified", "Sentence kanjified"),
                ("Sentence audio", "Sentence audio"),
            ],
        )

    def test_two_type_pairs_only_the_keys_both_blocks_name(self):
        self.assertEqual(
            roles.example_field_pairs(two_type_config(), VOCAB),
            [
                ("Text translation", "Example translation"),
                ("Text audio", "Example audio"),
            ],
        )

    def test_the_seen_count_is_never_copied(self):
        config = two_type_config()
        # Even named on the vocab block, the seen count is the sentence note's own
        config[VOCAB]["sentence_seen_count_field"] = "Seen count"
        self.assertNotIn("sentence_seen_count_field", roles.SENTENCE_KEYS)
        pairs = roles.example_field_pairs(config, VOCAB)
        self.assertNotIn("Seen count", [field for pair in pairs for field in pair])

    def test_a_type_without_the_vocab_role_is_refused(self):
        # In the two-type layout the sentence type is its own sentence type: its pairs would be
        # its fields onto themselves, a copy that quietly does nothing
        for config, name in [(two_type_config(), SENTENCE), (one_type_config(), "Kanji draw")]:
            with self.subTest(name):
                with self.assertRaises(roles.LayoutError) as raised:
                    roles.example_field_pairs(config, name)
                self.assertIn(f'"{name}" is not a vocab note type', str(raised.exception))
                with self.assertRaises(roles.LayoutError):
                    roles.example_id_field(config, name)

    def test_an_inconsistent_layout_is_refused(self):
        config = two_type_config()
        del config[SENTENCE]["vocab_note_type"]
        with self.assertRaises(roles.LayoutError):
            roles.example_field_pairs(config, VOCAB)


class CopyExampleTests(unittest.TestCase):
    def test_two_type_copies_translation_audio_and_the_id(self):
        sentence, vocab = sentence_note(), vocab_note()
        roles.copy_example(
            two_type_config(), VOCAB, sentence_note=sentence, sentence_id=1712, vocab_note=vocab
        )
        expected = vocab_note()
        expected["Example id"] = "1712"
        expected["Example translation"] = "text translation"
        expected["Example audio"] = "[sound:text.mp3]"
        self.assertEqual(vocab, expected)
        self.assertEqual(sentence, sentence_note())

    def test_two_type_without_an_id_field_copies_the_fields_only(self):
        config = two_type_config()
        del config[VOCAB]["example_sentence_id_field"]
        vocab = vocab_note()
        roles.copy_example(
            config, VOCAB, sentence_note=sentence_note(), sentence_id=1712, vocab_note=vocab
        )
        self.assertEqual(vocab["Example id"], "")
        self.assertEqual(vocab["Example translation"], "text translation")
        self.assertEqual(vocab["Example audio"], "[sound:text.mp3]")

    def test_one_type_copies_every_sentence_field_from_another_note(self):
        source, new = one_type_note("source"), one_type_note("new")
        roles.copy_example(
            one_type_config(), VOCAB, sentence_note=source, sentence_id=1712, vocab_note=new
        )
        expected = one_type_note("new")
        for field in [
            "Sentence",
            "Sentence furigana",
            "Sentence kanjified",
            "Sentence extraction",
            "Sentence translation",
            "Sentence audio",
        ]:
            expected[field] = f"source {field}"
        # The array, the word and the id field are the note's own: no field takes the id
        self.assertEqual(new, expected)
        self.assertEqual(source, one_type_note("source"))

    def test_one_type_onto_the_same_note_changes_nothing(self):
        note = one_type_note("same")
        roles.copy_example(
            one_type_config(), VOCAB, sentence_note=note, sentence_id=1712, vocab_note=note
        )
        self.assertEqual(note, one_type_note("same"))

    def test_a_field_missing_on_the_vocab_note_raises_before_any_write(self):
        vocab = vocab_note()
        del vocab["Example audio"]
        with self.assertRaises(roles.LayoutError) as raised:
            roles.copy_example(
                two_type_config(),
                VOCAB,
                sentence_note=sentence_note(),
                sentence_id=1712,
                vocab_note=vocab,
            )
        self.assertIn(f'"{VOCAB}"', str(raised.exception))
        self.assertIn("Example audio", str(raised.exception))
        # The translation comes first in the pairs and was not written either
        self.assertEqual(vocab["Example translation"], "old translation")
        self.assertEqual(vocab["Example id"], "")

    def test_a_missing_id_field_on_the_vocab_note_raises(self):
        vocab = vocab_note()
        del vocab["Example id"]
        with self.assertRaises(roles.LayoutError) as raised:
            roles.copy_example(
                two_type_config(),
                VOCAB,
                sentence_note=sentence_note(),
                sentence_id=1712,
                vocab_note=vocab,
            )
        self.assertIn("Example id", str(raised.exception))
        self.assertEqual(vocab["Example translation"], "old translation")

    def test_a_field_missing_on_the_sentence_note_raises(self):
        sentence = sentence_note()
        del sentence["Text translation"]
        vocab = vocab_note()
        with self.assertRaises(roles.LayoutError) as raised:
            roles.copy_example(
                two_type_config(), VOCAB, sentence_note=sentence, sentence_id=1712, vocab_note=vocab
            )
        self.assertIn(f'"{SENTENCE}"', str(raised.exception))
        self.assertIn("Text translation", str(raised.exception))
        self.assertEqual(vocab, vocab_note())

    def test_a_sentence_note_not_added_yet_is_not_linked(self):
        vocab = vocab_note()
        with self.assertRaises(ValueError):
            roles.copy_example(
                two_type_config(),
                VOCAB,
                sentence_note=sentence_note(),
                sentence_id=0,
                vocab_note=vocab,
            )
        self.assertEqual(vocab, vocab_note())

    def test_the_notes_are_keyword_only(self):
        # Both are the same shape; passed by position in the wrong order the sentence note
        # would be overwritten
        with self.assertRaises(TypeError):
            roles.copy_example(two_type_config(), VOCAB, sentence_note(), 1712, vocab_note())


class RoleErrorTests(unittest.TestCase):
    """Which notes an op of one role may run on: every type outside the two-type layout, as
    before roles existed, and in a pair only the type of the op's role."""

    def test_outside_the_two_type_layout_no_type_is_refused(self):
        config = one_type_config()
        for name in (VOCAB, "Kanji draw", "Basic", ""):
            for role in roles.ROLES:
                with self.subTest(name=name, role=role):
                    self.assertIsNone(roles.role_error(config, name, role))
            self.assertEqual(roles.roles_of(config, name), roles.ROLES)
        # A type of neither role in a two-type config is in no pair either
        self.assertEqual(roles.roles_of(two_type_config(), "Kanji draw"), roles.ROLES)

    def test_a_pair_type_is_refused_for_the_other_role_naming_both_types(self):
        config = two_type_config()
        self.assertIsNone(roles.role_error(config, SENTENCE, roles.SENTENCE_ROLE))
        self.assertIsNone(roles.role_error(config, VOCAB, roles.VOCAB_ROLE))
        self.assertEqual(
            roles.role_error(config, VOCAB, roles.SENTENCE_ROLE),
            f'This op runs on sentence notes, and "{VOCAB}" is the vocab note type of the'
            f' settings: run it on notes of "{SENTENCE}".',
        )
        self.assertEqual(
            roles.role_error(config, SENTENCE, roles.VOCAB_ROLE),
            f'This op runs on vocab notes, and "{SENTENCE}" is the sentence note type of the'
            f' settings: run it on notes of "{VOCAB}".',
        )
        self.assertEqual(roles.roles_of(config, SENTENCE), (roles.SENTENCE_ROLE,))
        self.assertEqual(roles.roles_of(config, VOCAB), (roles.VOCAB_ROLE,))

    def test_a_broken_pair_is_refused_for_both_roles_with_its_layout_error(self):
        config = two_type_config()
        del config[VOCAB]["sentence_note_type"]
        for name in (SENTENCE, VOCAB):
            error = roles.layout_error(config, name)
            self.assertIsNotNone(error)
            for role in roles.ROLES:
                with self.subTest(name=name, role=role):
                    self.assertEqual(roles.role_error(config, name, role), error)
            self.assertEqual(roles.roles_of(config, name), ())

    def test_an_unknown_role_is_a_bug(self):
        with self.assertRaises(ValueError):
            roles.role_error(two_type_config(), VOCAB, "sentences")


class NoteTypeSearchTests(unittest.TestCase):
    def test_a_plain_name_is_the_term_the_ops_have_always_built(self):
        self.assertEqual(roles.note_type_search(VOCAB), '"note:Japanese vocab note"')

    def test_what_anki_reads_as_syntax_is_escaped(self):
        # Each found exactly its own type in a real collection (Anki 26.09); unescaped, "a_b"
        # also found "axb" and the backslashes were an invalid search
        cases = {
            "a_b": '"note:a\\_b"',
            "star*name": '"note:star\\*name"',
            "back\\slash": '"note:back\\\\slash"',
            "trailing\\": '"note:trailing\\\\"',
            'Say "hi"': '"note:Say \\"hi\\""',
        }
        for name, term in cases.items():
            with self.subTest(name):
                self.assertEqual(roles.note_type_search(name), term)

    def test_colons_parentheses_and_hyphens_are_left_alone(self):
        # Inside the quotes Anki takes them as they are
        self.assertEqual(roles.note_type_search("colon:name (x) -y"), '"note:colon:name (x) -y"')


if __name__ == "__main__":
    unittest.main()
