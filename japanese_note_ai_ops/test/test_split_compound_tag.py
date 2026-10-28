"""The `vocab::split-compound-word` tag's sweep, as far as it is plain decisions: which notes
count as tagged, which forms a compound is listed by, what is written and when.

The collection here is a stand-in that answers a search with what the test hands it, so nothing
in this file says that a search finds the right notes. That, the tags really set, the undo step
and the config really written are test_anki/test_real_anki_split_compound_tag.py's, in a running
Anki.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from addon_modules import load_ops_module

sudachipy = ModuleType("sudachipy")
setattr(sudachipy, "Dictionary", object)
setattr(sudachipy, "SplitMode", object)
with mock.patch.dict(sys.modules, {"sudachipy": sudachipy}):
    tag = load_ops_module("split_compound_tag", subdir="")
match_flags = load_ops_module("match_flags", subdir="word_array")

CONFIG = {"Vocab": {"word_kanjified_field": "word", "word_list_field": "words"}}
COMPOUND = 1790025944461


def element(form, match_data=None, subs=None):
    return [form, "verb", form, "よみ", match_data or [], subs or []]


class FakeNote:
    def __init__(self, note_id, fields=None, tags=(), notetype="Vocab"):
        self.id = note_id
        self.fields = fields or {}
        self.tags = list(tags)
        self.notetype = notetype

    def note_type(self):
        return {"name": self.notetype} if self.notetype else None

    def has_tag(self, wanted):
        return wanted.lower() in (held.lower() for held in self.tags)

    def __contains__(self, field):
        return field in self.fields

    def __getitem__(self, field):
        return self.fields[field]


class FakeCollection:
    """`found` maps a piece of a search to the ids that search returns; the first piece a
    search contains answers it, and a search with none finds nothing."""

    def __init__(self, notes=(), found=None):
        self.notes = {note.id: note for note in notes}
        self.found = found or {}
        self.searches: list = []
        self.bulk_adds: list = []
        self.undo: list = []
        self.tags = SimpleNamespace(bulk_add=self.bulk_add)

    def find_notes(self, search):
        self.searches.append(search)
        for piece, ids in self.found.items():
            if piece in search:
                return list(ids)
        return []

    def get_note(self, note_id):
        return self.notes[note_id]

    def bulk_add(self, note_ids, tags):
        self.bulk_adds.append((list(note_ids), tags))
        return SimpleNamespace(count=len(note_ids))

    def add_custom_undo_entry(self, label):
        self.undo.append(("entry", label))
        return 7

    def merge_undo_entries(self, position):
        self.undo.append(("merge", position))
        return "merged changes"


def compound_note(word="毛羽立つ", tags=(tag.SPLIT_COMPOUND_TAG,), note_id=COMPOUND, **kwargs):
    return FakeNote(note_id, {"word": word, "words": ""}, tags, **kwargs)


def tagged(*notes):
    return FakeCollection(notes, {"tag:": [note.id for note in notes]})


class TaggedCompoundsTests(unittest.TestCase):
    def test_a_tagged_note_is_a_compound_by_its_word_field(self):
        [compound] = tag.tagged_compounds(tagged(compound_note()), CONFIG)

        self.assertEqual(compound, tag.Compound(COMPOUND, "毛羽立つ", "Vocab", "words"))

    def test_the_search_is_for_the_tag(self):
        col = tagged(compound_note())

        tag.tagged_compounds(col, CONFIG)

        self.assertEqual(col.searches, ['"tag:vocab::split-compound-word"'])

    def test_furigana_and_tags_in_the_word_field_are_not_part_of_the_word(self):
        for held in (" 毛羽[けば]立[た]つ", "<b>毛羽立つ</b>", " 毛羽立つ "):
            with self.subTest(held=held):
                [compound] = tag.tagged_compounds(tagged(compound_note(held)), CONFIG)
                self.assertEqual(compound.word, "毛羽立つ")

    def test_the_tag_counts_whatever_its_case(self):
        note = compound_note(tags=["Vocab::Split-Compound-Word"])

        self.assertEqual(len(tag.tagged_compounds(tagged(note), CONFIG)), 1)

    def test_a_note_with_only_a_child_of_the_tag_is_not_tagged(self):
        # Anki's tag search finds `tag::child` for `tag`, and a child says something else
        for held in ("vocab::split-compound-word::new", "vocab::split-compound-word::done"):
            with self.subTest(tag=held):
                col = tagged(compound_note(tags=[held]))
                self.assertEqual(tag.tagged_compounds(col, CONFIG), [])

    def test_a_note_with_no_word_to_list_is_passed_over(self):
        cases = {
            "an empty word": compound_note(""),
            "only furigana brackets": compound_note("<b></b>"),
            "a note type with no fields configured": compound_note(notetype="Basic"),
            "no note type": compound_note(notetype=None),
        }
        for name, note in cases.items():
            with self.subTest(case=name):
                self.assertEqual(tag.tagged_compounds(tagged(note), CONFIG), [])

    def test_a_note_type_configured_without_either_field_is_passed_over(self):
        for fields in ({"word_kanjified_field": "word"}, {"word_list_field": "words"}, {}):
            with self.subTest(fields=fields):
                col = tagged(compound_note())
                self.assertEqual(tag.tagged_compounds(col, {"Vocab": fields}), [])
        # ...and so is one whose word field the note does not have
        config = {"Vocab": {"word_kanjified_field": "missing", "word_list_field": "words"}}
        self.assertEqual(tag.tagged_compounds(tagged(compound_note()), config), [])


TWO_TYPE = {
    "Sentences": {"vocab_note_type": "Vocab", "word_list_field": "array"},
    "Vocab": {"sentence_note_type": "Sentences", "word_kanjified_field": "word", "word_sort_field": "s"},
}


class TwoTypeTests(unittest.TestCase):
    """In the two-type layout (note_roles) the tag is on a vocab note, whose type's settings name
    no word_list_field: the arrays linking it, searched and tagged, are its sentence type's. The
    vocab note keeps a field of the old array's name ("words" here) until the user deletes it,
    and that is never searched."""

    def test_a_tagged_vocab_note_is_a_compound_of_its_sentence_types_arrays(self):
        [compound] = tag.tagged_compounds(tagged(compound_note()), TWO_TYPE)

        self.assertEqual(compound, tag.Compound(COMPOUND, "毛羽立つ", "Sentences", "array"))

    def test_the_sentence_notes_are_searched_and_tagged(self):
        [compound] = tag.tagged_compounds(tagged(compound_note()), TWO_TYPE)
        linked = [element("毛羽立ち", [COMPOUND])]
        sentences = [
            FakeNote(5, {"array": match_flags.format_word_array(linked)}, notetype="Sentences"),
            FakeNote(6, {"array": match_flags.format_word_array(linked)}, notetype="Sentences"),
        ]
        col = FakeCollection(sentences, {'"note:Sentences" "array:re:': [5, 6]})

        listing = tag.list_compounds(col, [compound])

        self.assertEqual(listing.forms, {"毛羽立つ": ["毛羽立つ", "毛羽立ち"]})
        self.assertEqual(col.bulk_adds, [([5, 6], tag.REGENERATE_TAG)])
        self.assertTrue(col.searches)
        for search in col.searches:
            self.assertTrue(search.startswith('"note:Sentences" "array:re:'), search)

    def test_a_layout_whose_types_do_not_name_each_other_passes_the_note_over(self):
        broken = {
            "Sentences": {"word_list_field": "array"},
            "Vocab": dict(TWO_TYPE["Vocab"]),
        }

        with self.assertLogs(tag.logger, "WARNING"):
            self.assertEqual(tag.tagged_compounds(tagged(compound_note()), broken), [])


class NotListedTests(unittest.TestCase):
    def compounds(self):
        return [
            tag.Compound(1, "毛羽立つ", "Vocab", "words"),
            tag.Compound(2, "心地良い", "Vocab", "words"),
        ]

    def test_a_compound_the_config_lists_is_left_out(self):
        config = dict(CONFIG, split_compound_words=["毛羽立つ"])

        self.assertEqual([c.word for c in tag.not_listed(self.compounds(), config)], ["心地良い"])

    def test_the_list_is_read_as_the_generator_reads_it(self):
        config = dict(CONFIG, split_compound_words="毛羽立つ、心地良い")

        self.assertEqual(tag.not_listed(self.compounds(), config), [])

    def test_a_config_with_no_list_lists_none(self):
        self.assertEqual(len(tag.not_listed(self.compounds(), CONFIG)), 2)


class CompoundFormsTests(unittest.TestCase):
    COMPOUND_OF = tag.Compound(COMPOUND, "心地よい", "Vocab", "words")

    def sentence(self, note_id, array):
        return FakeNote(note_id, {"words": match_flags.format_word_array(array)})

    def forms(self, *sentences):
        col = FakeCollection(sentences, {"words:re:": [note.id for note in sentences]})
        return tag.compound_forms(col, self.COMPOUND_OF), col

    def test_with_no_array_linked_to_the_note_it_is_listed_by_its_own_word(self):
        forms, col = self.forms()

        self.assertEqual(forms, ["心地よい"])
        [search] = col.searches
        self.assertTrue(search.startswith('"note:Vocab" "words:re:'))
        self.assertIn(str(COMPOUND), search)

    def test_the_form_an_array_shows_the_linked_word_by_is_listed_too(self):
        # The generator looks a compound up by what its array element shows, and the note may
        # spell its word otherwise
        forms, _col = self.forms(self.sentence(5, [element("心地良い", [COMPOUND, 4])]))

        self.assertEqual(forms, ["心地よい", "心地良い"])

    def test_each_form_once_sub_words_included(self):
        forms, _col = self.forms(
            self.sentence(5, [element("心地良い", [COMPOUND])]),
            self.sentence(6, [element("心地良いと", subs=[element("心地良い", [COMPOUND, 2])])]),
            self.sentence(7, [element("心地よい", [COMPOUND])]),
        )

        self.assertEqual(forms, ["心地よい", "心地良い"])

    def test_an_element_linked_to_another_note_is_not_this_compound(self):
        forms, _col = self.forms(
            self.sentence(5, [element("音", [COMPOUND + 1]), element("良い", ["match"])])
        )

        self.assertEqual(forms, ["心地よい"])

    def test_a_field_that_holds_no_array_is_passed_over(self):
        broken = FakeNote(5, {"words": '[["心地良い", "adjective"'})
        empty = FakeNote(6, {"words": ""})

        forms, _col = self.forms(broken, empty)

        self.assertEqual(forms, ["心地よい"])


class LinkIdTests(unittest.TestCase):
    """A tagged note whose ID field holds a generated ID (link_ids.py) is linked by that: the
    arrays are searched for it and their elements holding it give the forms. Its note id is no
    link to it then."""

    LINK_ID = 1160697660039337
    CONFIG = {
        "Vocab": {
            "word_kanjified_field": "word",
            "word_list_field": "words",
            "new_note_id_field": "id",
        }
    }

    def test_a_compound_is_listed_by_the_forms_its_link_id_is_held_at(self):
        note = compound_note()
        note.fields["id"] = str(self.LINK_ID)
        [compound] = tag.tagged_compounds(tagged(note), self.CONFIG)
        self.assertEqual(compound, tag.Compound(self.LINK_ID, "毛羽立つ", "Vocab", "words"))
        linked = FakeNote(
            5, {"words": match_flags.format_word_array([element("毛羽立ち", [self.LINK_ID, 3])])}
        )
        by_note_id = FakeNote(
            6, {"words": match_flags.format_word_array([element("毛羽立って", [COMPOUND])])}
        )
        col = FakeCollection([linked, by_note_id], {"words:re:": [5, 6]})

        forms = tag.compound_forms(col, compound)

        self.assertEqual(forms, ["毛羽立つ", "毛羽立ち"])
        [search] = col.searches
        self.assertIn(str(self.LINK_ID), search)
        self.assertNotIn(str(COMPOUND), search)


class ListCompoundsTests(unittest.TestCase):
    def run_listing(self, compounds, whole):
        """`whole`: form -> the notes whose array has it whole."""
        col = FakeCollection()

        def find_notes(search):
            col.searches.append(search)
            for form, note_ids in whole.items():
                if match_flags.word_array_whole_word_regex(form) in search:
                    return list(note_ids)
            return []

        col.find_notes = find_notes
        return tag.list_compounds(col, compounds), col

    def test_the_notes_that_have_the_word_whole_are_tagged_in_one_undo_step(self):
        compounds = [tag.Compound(1, "毛羽立つ", "Vocab", "words")]

        listing, col = self.run_listing(compounds, {"毛羽立つ": [11, 12]})

        self.assertEqual(col.bulk_adds, [([11, 12], "sentence::regenerate")])
        self.assertEqual(col.undo, [("entry", tag.UNDO_LABEL), ("merge", 7)])
        self.assertEqual(listing.changes, "merged changes")
        self.assertEqual((listing.forms, listing.tagged), ({"毛羽立つ": ["毛羽立つ"]}, 2))

    def test_a_note_holding_two_of_the_compounds_is_tagged_once(self):
        compounds = [
            tag.Compound(1, "毛羽立つ", "Vocab", "words"),
            tag.Compound(2, "心地良い", "Vocab", "words"),
        ]

        listing, col = self.run_listing(compounds, {"毛羽立つ": [11, 12], "心地良い": [12, 13]})

        self.assertEqual(col.bulk_adds, [([11, 12, 13], "sentence::regenerate")])
        self.assertEqual(listing.forms, {"毛羽立つ": ["毛羽立つ"], "心地良い": ["心地良い"]})
        self.assertEqual(len(col.undo), 2)

    def test_two_notes_about_one_word_list_and_search_it_once(self):
        compounds = [
            tag.Compound(1, "毛羽立つ", "Vocab", "words"),
            tag.Compound(2, "毛羽立つ", "Vocab", "words"),
        ]

        listing, col = self.run_listing(compounds, {"毛羽立つ": [11]})

        self.assertEqual(listing.forms, {"毛羽立つ": ["毛羽立つ"]})
        whole = match_flags.word_array_whole_word_regex("毛羽立つ")
        self.assertEqual(len([search for search in col.searches if whole in search]), 1)

    def test_the_search_is_over_the_compounds_note_type_and_its_array_field(self):
        _listing, col = self.run_listing(
            [tag.Compound(1, "毛羽立つ", "Japanese vocab", "list")], {}
        )

        for search in col.searches:
            self.assertTrue(search.startswith('"note:Japanese vocab" "list:re:'), search)

    def test_a_word_no_array_has_whole_is_still_to_be_listed_and_makes_no_undo_step(self):
        listing, col = self.run_listing([tag.Compound(1, "毛羽立つ", "Vocab", "words")], {})

        self.assertEqual((listing.forms, listing.tagged), ({"毛羽立つ": ["毛羽立つ"]}, 0))
        self.assertEqual((col.bulk_adds, col.undo), ([], []))

    def test_what_is_listed_for_a_word_is_every_form_its_notes_come_to(self):
        # Two notes about one word, a meaning each, and an array links the second by another
        # spelling
        compounds = [
            tag.Compound(1, "心地よい", "Vocab", "words"),
            tag.Compound(2, "心地よい", "Vocab", "words"),
        ]
        linked = FakeNote(
            11, {"words": match_flags.format_word_array([element("心地良い", [2, 4])])}
        )
        col = FakeCollection([linked])
        col.find_notes = lambda search: (
            [11] if match_flags.word_array_link_regex(2) in search else []
        )

        listing = tag.list_compounds(col, compounds)

        self.assertEqual(listing.forms, {"心地よい": ["心地よい", "心地良い"]})


class AddToListTests(unittest.TestCase):
    def add(self, forms, user_config, defaults=None):
        merged = dict(defaults or {})
        merged.update(user_config)
        manager = mock.Mock()
        manager.getConfig.return_value = merged
        manager.addonFromModule.return_value = "the_addon"
        manager.addonMeta.return_value = {"config": dict(user_config), "mod": 5}
        with mock.patch.object(tag, "mw", SimpleNamespace(addonManager=manager)):
            added = tag.add_to_list(forms)
        return added, manager

    def test_the_forms_the_list_lacks_are_appended_to_it(self):
        added, manager = self.add(["心地良い", "毛羽立つ"], {"split_compound_words": ["毛羽立つ"]})

        self.assertEqual(added, ["心地良い"])
        [call] = manager.writeConfig.call_args_list
        self.assertEqual(call.args[1], {"split_compound_words": ["毛羽立つ", "心地良い"]})

    def test_only_this_key_is_added_to_what_the_user_set_themselves(self):
        # getConfig hands back the user's config over the shipped defaults. Written back whole,
        # every default would become the user's own setting and stop following the add-on's.
        defaults = {"split_compound_words": [], "proper_nouns_model": "a default", "retries": 5}

        _added, manager = self.add(["毛羽立つ"], {"retries": 9}, defaults)

        manager.addonMeta.assert_called_once_with("the_addon")
        [call] = manager.writeConfig.call_args_list
        self.assertEqual(call.args[1], {"retries": 9, "split_compound_words": ["毛羽立つ"]})

    def test_a_list_that_comes_from_the_defaults_is_kept_when_it_becomes_the_users(self):
        defaults = {"split_compound_words": ["仲睦まじい"]}

        added, manager = self.add(["毛羽立つ"], {}, defaults)

        self.assertEqual(added, ["毛羽立つ"])
        [call] = manager.writeConfig.call_args_list
        self.assertEqual(call.args[1], {"split_compound_words": ["仲睦まじい", "毛羽立つ"]})

    def test_nothing_is_written_when_the_list_has_them_all(self):
        added, manager = self.add(["毛羽立つ"], {"split_compound_words": "毛羽立つ 心地良い"})

        self.assertEqual(added, [])
        manager.writeConfig.assert_not_called()

    def add_with_meta_file(self, content):
        """`add_to_list` when Anki's `addonMeta` has nothing to say, which is its answer both
        for no meta.json (`content` None) and for one it could not read."""
        manager = mock.Mock()
        manager.getConfig.return_value = {"split_compound_words": []}
        manager.addonMeta.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            manager.addonsFolder.return_value = directory
            if content is not None:
                Path(directory, "meta.json").write_text(content, encoding="utf-8")
            with mock.patch.object(tag, "mw", SimpleNamespace(addonManager=manager)):
                added = tag.add_to_list(["毛羽立つ"])
        return added, manager

    def test_an_addon_with_no_config_of_the_users_yet_gets_one(self):
        for content in (None, "", "{}"):
            with self.subTest(meta_json=content):
                added, manager = self.add_with_meta_file(content)

                self.assertEqual(added, ["毛羽立つ"])
                written = manager.writeConfig.call_args.args[1]
                self.assertEqual(written, {"split_compound_words": ["毛羽立つ"]})

    def test_a_meta_json_that_cannot_be_read_is_not_written_over(self):
        # Half written by whatever is copying it, or broken by a hand edit: either way it holds
        # the user's settings, API keys among them, and writeConfig would leave only the list
        added, manager = self.add_with_meta_file('{"config": {"proper_nouns_model": "a mod')

        # None rather than an empty list: nothing was listed, and not because the list has it
        self.assertIsNone(added)
        manager.writeConfig.assert_not_called()


class SummaryTests(unittest.TestCase):
    def test_it_says_what_was_listed_and_how_many_notes_were_tagged(self):
        self.assertEqual(
            tag.summary(["毛羽立つ", "心地良い"], 3),
            "Compounds to split: 毛羽立つ, 心地良い listed. 3 notes tagged sentence::regenerate.",
        )
        self.assertEqual(tag.summary(["毛羽立つ"], 0), "Compounds to split: 毛羽立つ listed.")
        self.assertEqual(tag.summary([], 1), "1 note tagged sentence::regenerate.")


class FakeMainWindow:
    def __init__(self, col=None, config=None, media_syncing=False):
        self.col = col
        self.media_syncing = media_syncing
        self.timers: list = []
        self.media_syncer = SimpleNamespace(is_syncing=lambda: self.media_syncing)
        self.progress = SimpleNamespace(single_shot=lambda *args: self.timers.append(args))
        self.addonManager = mock.Mock()
        self.addonManager.getConfig.return_value = config


class WhenTheSweepRunsTests(unittest.TestCase):
    """Once a media sync has stopped, which is when Addon Config Sync has copied the configs
    back from the media folder, and never while one runs."""

    def setUp(self):
        self.mw = FakeMainWindow()
        patcher = mock.patch.object(tag, "mw", self.mw)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_collection_sync_with_no_media_sync_after_it_is_swept_at_once(self):
        tag.on_sync_did_finish()

        # From a timer of Anki's, which needs the collection
        self.assertEqual(self.mw.timers, [(tag.SWEEP_DELAY_MS, tag.sweep, True)])

    def test_nothing_is_swept_while_the_media_sync_runs(self):
        self.mw.media_syncing = True

        tag.on_sync_did_finish()
        tag.on_media_sync_did_start_or_stop(True)

        self.assertEqual(self.mw.timers, [])

    def test_it_is_swept_when_the_media_sync_stops(self):
        self.mw.media_syncing = True
        tag.on_sync_did_finish()
        self.mw.media_syncing = False

        tag.on_media_sync_did_start_or_stop(False)

        # ...from a timer, so after the other handlers of that hook call
        self.assertEqual(self.mw.timers, [(tag.SWEEP_DELAY_MS, tag.sweep, True)])

    def test_a_media_sync_no_collection_sync_started_is_swept_after_as_well(self):
        # Anki's periodic one brings no tags, but an older Addon Config Sync copies the configs
        # back after it all the same, and a word listed since the last collection sync began
        # is off the list again
        tag.on_media_sync_did_start_or_stop(True)
        tag.on_media_sync_did_start_or_stop(False)

        self.assertEqual(self.mw.timers, [(tag.SWEEP_DELAY_MS, tag.sweep, True)])

    def test_both_hooks_are_registered(self):
        finished, media = mock.Mock(), mock.Mock()
        hooks = SimpleNamespace(sync_did_finish=finished, media_sync_did_start_or_stop=media)
        with mock.patch.object(tag, "gui_hooks", hooks):
            tag.init_split_compound_tag()

        finished.append.assert_called_once_with(tag.on_sync_did_finish)
        media.append.assert_called_once_with(tag.on_media_sync_did_start_or_stop)


class FakeCollectionOp:
    """Runs the op at once on the collection it is handed, then the success handler."""

    started: list = []

    def __init__(self, parent, op):
        self.parent, self.op = parent, op

    def success(self, handler):
        self.handler = handler
        return self

    def run_in_background(self):
        FakeCollectionOp.started.append(self)
        self.handler(self.op(self.parent.col))


class SweepTests(unittest.TestCase):
    def setUp(self):
        FakeCollectionOp.started = []
        self.tooltips: list = []
        self.logs: list = []
        # What the module remembers of a session of Anki's, which a test is the whole of
        tag._listed_this_session.clear()
        self.addCleanup(tag._listed_this_session.clear)
        for name, value in (
            ("CollectionOp", FakeCollectionOp),
            ("tooltip", lambda text, **kwargs: self.tooltips.append(text)),
            ("start_call_log", self.logs.append),
        ):
            patcher = mock.patch.object(tag, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sweep(self, col, config):
        window = FakeMainWindow(col, config)
        window.addonManager.addonMeta.return_value = {"config": dict(config or {})}
        with mock.patch.object(tag, "mw", window):
            tag.sweep()
        return window

    def test_a_tagged_note_whose_word_is_not_listed_is_listed_and_said_so(self):
        col = tagged(compound_note())

        window = self.sweep(col, CONFIG)

        self.assertEqual(len(FakeCollectionOp.started), 1)
        written = window.addonManager.writeConfig.call_args.args[1]
        self.assertEqual(written["split_compound_words"], ["毛羽立つ"])
        self.assertEqual(self.tooltips, ["Compounds to split: 毛羽立つ listed."])
        self.assertEqual(self.logs, ["split_compound_tag"])

    def test_a_sync_with_nothing_new_starts_no_operation_and_opens_no_log(self):
        cases = {
            "no note is tagged": (FakeCollection(), CONFIG),
            "every tagged word is listed": (
                tagged(compound_note()),
                dict(CONFIG, split_compound_words=["毛羽立つ"]),
            ),
        }
        for name, (col, config) in cases.items():
            with self.subTest(case=name):
                window = self.sweep(col, config)

                self.assertEqual(FakeCollectionOp.started, [])
                window.addonManager.writeConfig.assert_not_called()
                self.assertEqual((self.tooltips, self.logs), ([], []))

    def whole_in(self, *note_ids):
        """A collection with the tagged compound's note and these notes having 毛羽立つ whole."""
        col = tagged(compound_note())
        col.found[match_flags.word_array_whole_word_regex("毛羽立つ")] = list(note_ids)
        return col

    def test_a_word_that_fell_off_the_list_is_put_back_without_a_word_or_a_tag(self):
        # Addon Config Sync has copied the config from before the listing back over this
        # device's, which it can do after every media sync until a collection sync starts
        col = self.whole_in(11, 12)
        self.sweep(col, CONFIG)
        self.assertEqual((len(FakeCollectionOp.started), len(col.bulk_adds)), (1, 1))
        self.assertEqual(len(self.tooltips), 1)

        window = self.sweep(col, CONFIG)

        written = window.addonManager.writeConfig.call_args.args[1]
        self.assertEqual(written["split_compound_words"], ["毛羽立つ"])
        # ...and nothing else: no operation, so no notes tagged a second time, though both
        # still have the word whole, and nothing said or logged again
        self.assertEqual((len(FakeCollectionOp.started), len(col.bulk_adds)), (1, 1))
        self.assertEqual((len(self.tooltips), self.logs), (1, ["split_compound_tag"]))

    def test_every_form_listed_for_the_word_is_put_back(self):
        linked = FakeNote(
            11, {"words": match_flags.format_word_array([element("毛羽立つ", [COMPOUND, 4])])}
        )
        col = FakeCollection(
            [compound_note("毛羽だつ"), linked],
            {"tag:": [COMPOUND], match_flags.word_array_link_regex(COMPOUND): [11]},
        )
        self.sweep(col, CONFIG)

        window = self.sweep(col, CONFIG)

        written = window.addonManager.writeConfig.call_args.args[1]
        self.assertEqual(written["split_compound_words"], ["毛羽だつ", "毛羽立つ"])
        self.assertEqual(len(FakeCollectionOp.started), 1)

    def test_in_the_next_session_a_word_off_the_list_is_listed_as_if_new(self):
        col = self.whole_in(11)
        self.sweep(col, CONFIG)

        tag._listed_this_session.clear()  # Anki restarted
        self.sweep(col, CONFIG)

        self.assertEqual((len(FakeCollectionOp.started), len(col.bulk_adds)), (2, 2))
        self.assertEqual(len(self.tooltips), 2)

    def test_a_list_that_could_not_be_written_is_not_remembered_as_listed(self):
        col = self.whole_in(11)
        unwritten = mock.patch.object(tag, "users_config", return_value=None)

        with unwritten:
            window = self.sweep(col, CONFIG)

        window.addonManager.writeConfig.assert_not_called()
        self.assertEqual(tag._listed_this_session, {})
        # The notes were tagged all the same, and said so; the next sweep lists the word
        self.assertEqual(self.tooltips, ["1 note tagged sentence::regenerate."])

    def test_a_word_put_back_beside_a_new_one_leaves_the_new_one_its_full_sweep(self):
        col = self.whole_in(11)
        self.sweep(col, CONFIG)
        new = compound_note("心地良い", note_id=COMPOUND + 1)
        col.notes[new.id] = new
        col.found["tag:"] = [COMPOUND, new.id]
        col.found[match_flags.word_array_whole_word_regex("心地良い")] = [21]

        window = self.sweep(col, CONFIG)

        self.assertEqual(len(FakeCollectionOp.started), 2)
        self.assertEqual(col.bulk_adds[-1], ([21], "sentence::regenerate"))
        writes = window.addonManager.writeConfig.call_args_list
        # (The stand-in config is never changed by a write, so each starts from no list)
        self.assertEqual(
            [call.args[1]["split_compound_words"] for call in writes], [["毛羽立つ"], ["心地良い"]]
        )

    def test_no_collection_or_no_config_is_nothing_to_do(self):
        for col, config in ((None, CONFIG), (tagged(compound_note()), None)):
            with self.subTest(col=col is not None):
                self.sweep(col, config)

                self.assertEqual(FakeCollectionOp.started, [])


if __name__ == "__main__":
    unittest.main()
