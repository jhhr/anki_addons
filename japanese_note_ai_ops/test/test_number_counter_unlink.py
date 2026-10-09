"""Unlinking the number-and-counter words the judge no longer matches, and deleting the notes
that were added for them: which words are asked about, which notes go, what is written."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import number_counter_unlink as unlink  # noqa: E402

MODEL = "model"
ADDED = [unlink.ADDED_TAG]


def word(pos: str, form: str, match_data=None, sub_words=None) -> list:
    return [form, pos, form, form + "よみ", list(match_data or []), sub_words or []]


def counted(form: str, match_data=None, pos: str = "noun") -> list:
    """A word made of a number and a counter, both linked to notes of their own."""
    subs = [word("number", form[0], [1, 5]), word("counter", form[1:], [2, 5])]
    return word(pos, form, match_data, subs)


def array(*words) -> str:
    return json.dumps(list(words), ensure_ascii=False)


def note(nid: int, *words, tags=()) -> dict:
    return {"nid": nid, "tags": list(tags), "sentence-vocab-list": array(*words)}


def answers_for(rows: list, decisions: dict) -> dict:
    """The answers file's content for the rows' words: `{form: decision}`, one for all of a
    form's sentences, or `{(note id, form): decision}` for one sentence."""
    answers = {}
    arrays = unlink.read_arrays(rows)
    split = frozenset(unlink.split_words(arrays))
    for nid, arr in arrays.items():
        for found in unlink.counted_words(nid, arr, split):
            form = found.elem[2]
            answer = decisions.get((nid, form), decisions.get(form))
            if answer:
                answers[unlink.prompt_key(MODEL, found.prompt)] = answer
    return answers


class FakeAnki:
    def __init__(self, rows: list):
        self.notes = {
            row["nid"]: {
                "noteId": row["nid"],
                "tags": list(row.get("tags", [])),
                "fields": {unlink.ARRAY_FIELD: {"value": row[unlink.ARRAY_FIELD]}},
            }
            for row in rows
        }
        self.deleted: list = []
        self.searches: list = []
        self.studied: set = set()

    def notes_info(self, nids):
        return [self.notes.get(n, {}) for n in nids]

    def update_note_fields(self, nid, fields):
        for name, value in fields.items():
            self.notes[nid]["fields"][name] = {"value": value}

    def find_notes(self, query):
        if query.startswith("-is:new nid:"):
            asked = {int(nid) for nid in query.rsplit(":", 1)[1].split(",")}
            return sorted(asked & self.studied)
        self.searches.append(query)
        needle = query.strip('"').split(":", 1)[1].strip("*")
        return [
            nid
            for nid, info in self.notes.items()
            if needle in info["fields"][unlink.ARRAY_FIELD]["value"]
        ]

    def delete_notes(self, nids):
        self.deleted.extend(nids)
        for nid in nids:
            del self.notes[nid]

    def array(self, nid: int) -> list:
        return json.loads(self.notes[nid]["fields"][unlink.ARRAY_FIELD]["value"])


def states(array: list) -> dict:
    return {elem[2]: elem[4] for _, elem in unlink.match_flags.iter_words(array)}


class TestWhichWordsAreAskedAbout(unittest.TestCase):
    def test_a_judged_number_and_counter_word_at_any_depth(self):
        inner = counted("一回", [70, 5])
        rows = [
            note(
                9,
                counted("三階", [50, 5]),
                counted("二度", [60], pos="adverb"),
                counted("五本", ["match"]),
                word("expression", "もう一回", ["dontmatch"], [word("adverb", "もう", [3, 5]), inner]),
            )
        ]
        found = unlink.counted_words(9, unlink.read_arrays(rows)[9])
        self.assertEqual([w.elem[2] for w in found], ["三階", "二度", "五本", "一回"])
        self.assertEqual([w.linked for w in found], [50, 60, None, 70])
        self.assertIn("<b>一回</b>", found[3].sentence)
        self.assertIn("Part of: もう一回", found[3].prompt)

    def test_not_a_word_still_unjudged_or_already_dontmatch_or_of_another_shape(self):
        rows = [
            note(
                9,
                counted("三階"),
                counted("二回", ["dontmatch"]),
                word("noun", "手紙", [5, 5], [word("noun", "手", [6, 5]), word("noun", "紙", [7, 5])]),
                word("noun", "第三者", [8, 5], [word("prefix", "第"), *counted("三者")[5]]),
            )
        ]
        self.assertEqual(unlink.counted_words(9, unlink.read_arrays(rows)[9]), [])


class TestThePlan(unittest.TestCase):
    def plan(self, rows: list, decisions: dict):
        return unlink.plan(rows, answers_for(rows, decisions), MODEL)

    def test_the_judges_answer_in_each_sentence_decides(self):
        rows = [
            note(1, counted("一杯", [50, 5])),
            note(2, counted("一杯", [51, 5]), word("noun", "水", [4, 5])),
            note(3, counted("二回", [52, 5])),
        ]
        planned = self.plan(rows, {(1, "一杯"): "match", (2, "一杯"): "dontmatch"})
        self.assertEqual([(w.nid, w.elem[2]) for w in planned.unlink], [(2, "一杯")])
        self.assertEqual([(w.nid, w.elem[2]) for w in planned.kept], [(1, "一杯")])
        self.assertEqual([(w.nid, w.elem[2]) for w in planned.unasked], [(3, "二回")])

    def test_a_note_added_for_the_word_goes_once_nothing_links_it(self):
        # 50 was copied from note 1, array and all, so its own array links it too
        rows = [
            note(1, counted("三階", [50, 5])),
            note(50, counted("三階", [50, 5]), tags=ADDED),
        ]
        planned = self.plan(rows, {"三階": "dontmatch"})
        self.assertEqual(planned.delete, [50])
        self.assertEqual(planned.held, {})
        # its own word is not rewritten: the array goes with the note
        self.assertEqual([w.nid for w in planned.unlink], [1])
        self.assertEqual([w.nid for w in planned.own], [50])

    def test_a_note_that_came_with_a_deck_stays(self):
        rows = [note(1, counted("三階", [50, 5])), note(50, counted("三階", [50, 5]))]
        planned = self.plan(rows, {"三階": "dontmatch"})
        self.assertEqual(planned.delete, [])
        self.assertEqual(planned.held, {unlink.NOT_ADDED: [50]})
        # the other sentence's link goes all the same, and its own word stays its own
        self.assertEqual([w.nid for w in planned.unlink], [1])
        self.assertEqual([w.nid for w in planned.own], [50])

    def test_a_note_that_only_its_own_word_was_turned_down_for_is_not_listed(self):
        # A deck's note whose example the judge dislikes: nothing of it changes
        rows = [note(50, counted("一種", [50, 5]))]
        planned = self.plan(rows, {"一種": "dontmatch"})
        self.assertEqual((planned.unlink, planned.delete, planned.held), ([], [], {}))
        self.assertEqual([w.nid for w in planned.own], [50])

    def test_an_added_note_only_its_own_array_links_goes(self):
        # Every other sentence was already set to dontmatch by hand
        rows = [
            note(1, counted("三階", ["dontmatch"])),
            note(50, counted("三階", [50, 5]), tags=ADDED),
        ]
        planned = self.plan(rows, {"三階": "dontmatch"})
        self.assertEqual((planned.unlink, planned.delete), ([], [50]))

    def test_a_note_a_kept_sentence_or_another_word_still_links_stays(self):
        rows = [
            note(1, counted("一杯", [50, 5])),
            note(2, counted("一杯", [50, 5]), word("noun", "酒", [4, 5])),
            note(3, counted("二回", [60, 5])),
            # another word, never asked about
            note(4, word("noun", "二回目", [60, 5])),
            note(50, word("noun", "杯", [2, 5]), tags=ADDED),
            note(60, word("noun", "回", [2, 5]), tags=ADDED),
        ]
        decisions = {(1, "一杯"): "dontmatch", (2, "一杯"): "match", "二回": "dontmatch"}
        planned = self.plan(rows, decisions)
        self.assertEqual(planned.delete, [])
        self.assertEqual(planned.held, {unlink.STILL_LINKED: [50, 60]})

    def test_a_note_whose_own_sentence_keeps_the_word_stays(self):
        rows = [
            note(1, counted("一番", [50, 5]), word("noun", "窓口", [4, 5])),
            note(50, counted("一番", [50, 5]), tags=ADDED),
        ]
        planned = self.plan(rows, {(1, "一番"): "dontmatch", (50, "一番"): "match"})
        self.assertEqual(planned.delete, [])
        self.assertEqual(planned.held, {unlink.STILL_LINKED: [50]})

    def test_two_added_notes_that_only_link_each_other_both_go(self):
        # Each names the other by a word the judge is not asked about, so those links stay
        rows = [
            note(1, counted("三階", [50, 5]), counted("二回", [60, 5])),
            note(50, word("noun", "回数", [60, 5]), tags=ADDED),
            note(60, word("noun", "階段", [50, 5]), tags=ADDED),
        ]
        planned = self.plan(rows, {"三階": "dontmatch", "二回": "dontmatch"})
        self.assertEqual(planned.delete, [50, 60])
        self.assertEqual(planned.held, {})

    def test_a_studied_note_stays_and_keeps_what_its_array_links(self):
        rows = [
            note(1, counted("三階", [50, 5]), counted("二回", [60, 5])),
            {**note(50, word("noun", "回数", [60, 5]), tags=ADDED), "studied": True},
            note(60, word("noun", "階段", [4, 5]), tags=ADDED),
        ]
        planned = self.plan(rows, {"三階": "dontmatch", "二回": "dontmatch"})
        self.assertEqual(planned.delete, [])
        self.assertEqual(planned.held, {unlink.STUDIED: [50], unlink.STILL_LINKED: [60]})

    def test_a_whole_word_is_asked_about_when_some_array_holds_it_split(self):
        # 九[きゅう] 人[にん] in note 1, 九人[きゅうにん] in note 2: the same count
        other_reading = ["九人", "noun", "九人", "くにん", [50, 5], []]
        rows = [
            note(1, counted("九人", [50, 5])),
            note(2, word("noun", "九人", [50, 5]), word("noun", "一緒", [4, 5]), other_reading),
            note(50, word("noun", "人", [2, 5]), tags=ADDED),
        ]
        arrays = unlink.read_arrays(rows)
        split = frozenset(unlink.split_words(arrays))
        self.assertEqual(split, {("九人", "九人よみ")})
        whole = unlink.counted_words(2, arrays[2], split)
        self.assertEqual([(w.elem[2], w.elem[3]) for w in whole], [("九人", "九人よみ")])
        # asked as the judge would ask about it: under its own group's rules
        self.assertIn(unlink.judge.POS_RULES["noun-main"], whole[0].prompt)

        planned = self.plan(rows, {"九人": "dontmatch"})
        self.assertEqual(sorted(w.nid for w in planned.unlink), [1, 2])
        # the other reading's link is not this script's business, and it keeps the note
        self.assertEqual(planned.held, {unlink.STILL_LINKED: [50]})

    def test_a_note_linked_from_a_note_that_stays_is_not_deleted_with_the_rest(self):
        # 60's array links 50 by another word, and 60 itself is kept by note 2
        rows = [
            note(1, counted("三階", [50, 5]), counted("二回", [60, 5])),
            note(2, word("noun", "二回目", [60, 5])),
            note(50, counted("三階", [50, 5]), tags=ADDED),
            note(60, word("noun", "階段", [50, 5]), tags=ADDED),
        ]
        planned = self.plan(rows, {"三階": "dontmatch", "二回": "dontmatch"})
        self.assertEqual(planned.delete, [])
        self.assertEqual(planned.held, {unlink.STILL_LINKED: [50, 60]})

    def test_a_word_with_no_answer_keeps_its_note(self):
        rows = [
            note(1, counted("三階", [50, 5])),
            note(2, counted("三階", [50, 5]), word("noun", "家", [4, 5])),
            note(50, word("noun", "階", [2, 5]), tags=ADDED),
        ]
        planned = self.plan(rows, {(1, "三階"): "dontmatch"})
        self.assertEqual([w.nid for w in planned.unasked], [2])
        self.assertEqual(planned.delete, [])

    def test_the_report_names_what_goes_and_what_stays(self):
        rows = [
            note(1, counted("三階", [50, 5]), counted("二回", [60, 5])),
            note(2, counted("三つ", [70, 5])),
            {**note(50, counted("三階", [50, 5]), tags=ADDED), "vocab-key": "三階"},
            {**note(60, word("noun", "回", [2, 5]), tags=ADDED), "vocab-key": "二回", "studied": True},
        ]
        planned = self.plan(rows, {"三階": "dontmatch", "二回": "dontmatch", "三つ": "match"})
        lines = unlink.report(planned, rows)
        text = "\n".join(lines)
        self.assertIn("2 words in 1 word arrays go to dontmatch", lines[0])
        self.assertIn("1 notes are deleted", lines[0])
        self.assertIn("1 more the judge said dontmatch about are a note's own word", lines[2])
        self.assertIn("三階 [三階よみ] x1", text)
        self.assertIn("三つ [三つよみ] x1", text)
        self.assertEqual(lines[lines.index("--- notes deleted (1) ---") + 1].strip(), "50 三階 []")
        self.assertIn("(1): %s ---" % unlink.STUDIED, text)
        self.assertIn("60 二回 [] studied", text)


class TestWriting(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.undo = Path(folder.name) / "undo.jsonl"
        self.deleted = Path(folder.name) / "deleted.jsonl"

    def run_apply(self, rows: list, decisions: dict, anki=None):
        answers = answers_for(rows, decisions)
        planned = unlink.plan(rows, answers, MODEL)
        anki = anki or FakeAnki(rows)
        result = unlink.apply(anki, planned, answers, MODEL, self.undo, self.deleted)
        return anki, result

    def test_the_word_goes_to_dontmatch_and_its_parts_keep_their_notes(self):
        rows = [
            note(1, counted("三階", [50, 5]), word("noun", "家", [4, 5])),
            note(50, counted("三階", [50, 5]), tags=ADDED),
        ]
        anki, (written, gone, refused) = self.run_apply(rows, {"三階": "dontmatch"})
        self.assertEqual((written, gone, refused), (1, 1, []))
        self.assertEqual(
            states(anki.array(1)),
            {"三階": ["dontmatch"], "三": [1, 5], "階": [2, 5], "家": [4, 5]},
        )
        self.assertEqual(anki.deleted, [50])
        # what the note held is on disk, and so is note 1's field as it was
        kept = [json.loads(line) for line in self.deleted.read_text("utf-8").splitlines()]
        self.assertEqual([info["noteId"] for info in kept], [50])
        self.assertEqual(kept[0]["tags"], ADDED)
        undo = [json.loads(line) for line in self.undo.read_text("utf-8").splitlines()]
        self.assertEqual(undo[0]["before"], rows[0][unlink.ARRAY_FIELD])

    def test_an_array_that_changed_since_the_dump_is_judged_as_it_is_now(self):
        rows = [
            note(1, counted("三階", [50, 5])),
            note(2, counted("三階", [50, 5]), word("noun", "家", [4, 5])),
            note(50, counted("三階", [50, 5]), tags=ADDED),
        ]
        anki = FakeAnki(rows)
        # Since the dump, note 2's sentence was edited: its word has no answer any more
        edited = array(counted("三階", [50, 5]), word("noun", "庭", [4, 5]))
        anki.update_note_fields(2, {unlink.ARRAY_FIELD: edited})
        anki, (written, gone, refused) = self.run_apply(rows, {"三階": "dontmatch"}, anki)
        self.assertEqual((written, gone), (1, 0))
        self.assertEqual(states(anki.array(2))["三階"], [50, 5])
        self.assertEqual(anki.deleted, [])
        self.assertTrue(any("nid 2: no word left" in line for line in refused))
        # and the note it still names is asked about in Anki, not taken from the dump
        self.assertTrue(any("nid 50: not deleted, still named" in line for line in refused))
        self.assertEqual(anki.searches, ['"sentence-vocab-list:*50*"'])

    def test_a_note_that_lost_the_added_tag_is_not_deleted(self):
        rows = [note(1, counted("三階", [50, 5])), note(50, counted("三階", [50, 5]), tags=ADDED)]
        anki = FakeAnki(rows)
        anki.notes[50]["tags"] = []
        anki, (written, gone, refused) = self.run_apply(rows, {"三階": "dontmatch"}, anki)
        self.assertEqual((written, gone), (1, 0))
        self.assertEqual(anki.deleted, [])
        self.assertTrue(any("nid 50: not deleted, it no longer has the tag" in r for r in refused))

    def test_a_note_that_stays_keeps_its_own_word(self):
        # The deck's own note for the word: the other sentence's link goes, its own stays
        rows = [note(1, counted("三階", [50, 5])), note(50, counted("三階", [50, 5]))]
        anki, (written, gone, refused) = self.run_apply(rows, {"三階": "dontmatch"})
        self.assertEqual((written, gone, refused), (1, 0, []))
        self.assertEqual(states(anki.array(1))["三階"], ["dontmatch"])
        self.assertEqual(states(anki.array(50))["三階"], [50, 5])

    def test_a_rewrite_that_did_not_land_is_reported_and_keeps_the_note(self):
        """AnkiConnect reports no error for a note open in the browser's editor, which it
        does not update."""
        rows = [note(1, counted("三階", [50, 5])), note(50, counted("三階", [50, 5]), tags=ADDED)]

        class EditorOpen(FakeAnki):
            def update_note_fields(self, nid, fields):
                pass

        anki, (written, gone, refused) = self.run_apply(rows, {"三階": "dontmatch"}, EditorOpen(rows))
        self.assertEqual(gone, 0)
        self.assertEqual(anki.deleted, [])
        self.assertIn("nid 1: the rewrite did not land; is the note open in an editor?", refused)
        self.assertIn("nid 50: not deleted, still named in the array of 1", refused)

    def test_a_note_studied_since_the_dump_is_not_deleted(self):
        rows = [note(1, counted("三階", [50, 5])), note(50, counted("三階", [50, 5]), tags=ADDED)]
        anki = FakeAnki(rows)
        anki.studied = {50}
        anki, (written, gone, refused) = self.run_apply(rows, {"三階": "dontmatch"}, anki)
        self.assertEqual((written, gone), (1, 0))
        self.assertEqual(anki.deleted, [])
        self.assertEqual(refused, ["nid 50: not deleted, it has been studied since the dump"])

    def test_a_note_that_stays_after_all_keeps_the_notes_its_array_names(self):
        # 50 is only linked from 60, so both go by the dump; but 60 stays in the end
        rows = [
            note(1, counted("三階", [50, 5]), counted("二回", [60, 5])),
            note(50, word("noun", "階", [2, 5]), tags=ADDED),
            note(60, word("noun", "階段", [50, 5]), tags=ADDED),
        ]
        decisions = {"三階": "dontmatch", "二回": "dontmatch"}
        planned = unlink.plan(rows, answers_for(rows, decisions), MODEL)
        self.assertEqual(planned.delete, [50, 60])
        anki = FakeAnki(rows)
        anki.notes[60]["tags"] = []
        anki, (written, gone, refused) = self.run_apply(rows, decisions, anki)
        self.assertEqual((written, gone), (1, 0))
        self.assertEqual(anki.deleted, [])
        self.assertTrue(any("nid 60: not deleted, it no longer has the tag" in r for r in refused))
        self.assertIn("nid 50: not deleted, still named in the array of 60", refused)

    def test_revert_puts_the_arrays_back_and_skips_a_note_that_is_gone(self):
        rows = [
            note(1, counted("三階", [50, 5])),
            note(2, counted("三階", [50, 5])),
            note(50, counted("三階", [50, 5]), tags=ADDED),
        ]
        anki, (written, gone, _) = self.run_apply(rows, {"三階": "dontmatch"})
        self.assertEqual((written, gone), (2, 1))
        anki.delete_notes([2])
        reverted, refused = unlink.revert(anki, self.undo)
        self.assertEqual(reverted, 1)
        # back to the id of the note that was deleted: the next match run puts it to match
        self.assertEqual(states(anki.array(1))["三階"], [50, 5])
        self.assertEqual(refused, ["nid 2: the note is gone, nothing to put back"])
        self.assertEqual(self.undo.read_text("utf-8"), "")

    def run_restore(self, anki, decisions: dict, apply: bool = True):
        """`--restore` on what the undo file holds, the judge now answering `decisions` by
        word."""
        entries = unlink.read_jsonl(self.undo)
        nids = sorted({entry["nid"] for entry in entries})
        turned, arrays, skipped = unlink.turned_words(entries, anki.notes_info(nids))
        answers = {
            unlink.prompt_key(MODEL, word.prompt): decisions[word.elem[2]]
            for word, _ in turned
            if word.elem[2] in decisions
        }
        back, stay, unasked = unlink.plan_restore(turned, answers, MODEL)
        undo = self.undo if apply else None
        written, put, failed = unlink.restore(anki, entries, arrays, back, undo)
        return written, put, stay, unasked, skipped + failed

    def test_restore_gives_back_the_link_the_rules_now_match_and_nothing_else(self):
        rows = [
            note(1, counted("一杯", [50, 4]), counted("三階", [60, 5]), word("noun", "水", [4, 5])),
            note(50, word("noun", "杯", [2, 5])),
            note(60, word("noun", "階", [2, 5])),
        ]
        anki, _ = self.run_apply(rows, {"一杯": "dontmatch", "三階": "dontmatch"})
        self.assertEqual(states(anki.array(1))["一杯"], ["dontmatch"])

        written, put, stay, unasked, problems = self.run_restore(
            anki, {"一杯": "match", "三階": "dontmatch"}
        )
        self.assertEqual((written, unasked, problems), (1, [], []))
        self.assertEqual([(w.elem[2], data) for w, data in put], [("一杯", [50, 4])])
        self.assertEqual([w.elem[2] for w in stay], ["三階"])
        self.assertEqual(
            states(anki.array(1)),
            {"一杯": [50, 4], "一": [1, 5], "杯": [2, 5], "三階": ["dontmatch"], "三": [1, 5],
             "階": [2, 5], "水": [4, 5]},
        )
        # the undo file follows, so a revert still puts the rest back
        reverted, refused = unlink.revert(anki, self.undo)
        self.assertEqual((reverted, refused), (1, []))
        self.assertEqual(states(anki.array(1))["三階"], [60, 5])

    def test_restore_without_apply_writes_nothing(self):
        rows = [note(1, counted("一杯", [50, 4])), note(50, word("noun", "杯", [2, 5]))]
        anki, _ = self.run_apply(rows, {"一杯": "dontmatch"})
        undo_before = self.undo.read_text("utf-8")
        written, put, _, _, _ = self.run_restore(anki, {"一杯": "match"}, apply=False)
        self.assertEqual((written, len(put)), (1, 1))
        self.assertEqual(states(anki.array(1))["一杯"], ["dontmatch"])
        self.assertEqual(self.undo.read_text("utf-8"), undo_before)

    def test_restore_drops_the_undo_entry_of_an_array_that_is_all_back(self):
        rows = [note(1, counted("一杯", [50, 4])), note(50, word("noun", "杯", [2, 5]))]
        anki, _ = self.run_apply(rows, {"一杯": "dontmatch"})
        self.run_restore(anki, {"一杯": "match"})
        self.assertEqual(anki.array(1), json.loads(rows[0][unlink.ARRAY_FIELD]))
        self.assertEqual(self.undo.read_text("utf-8"), "")

    def test_restore_puts_a_word_whose_note_was_deleted_back_to_match(self):
        # The run deleted the note it had added for one o'clock; the next match run makes it
        rows = [
            note(1, counted("一時", [50, 5])),
            note(50, counted("一時", [50, 5]), tags=ADDED),
        ]
        anki, (_, gone, _) = self.run_apply(rows, {"一時": "dontmatch"})
        self.assertEqual(gone, 1)
        written, put, _, _, problems = self.run_restore(anki, {"一時": "match"})
        self.assertEqual((written, problems), (1, []))
        self.assertEqual(states(anki.array(1))["一時"], ["match"])

    def test_restore_leaves_an_element_that_changed_since(self):
        rows = [note(1, counted("一杯", [50, 4])), note(50, word("noun", "杯", [2, 5]))]
        anki, _ = self.run_apply(rows, {"一杯": "dontmatch"})
        # judged by hand since: no longer the dontmatch the run left
        anki.update_note_fields(1, {unlink.ARRAY_FIELD: array(counted("一杯", ["match"]))})
        written, put, _, _, problems = self.run_restore(anki, {"一杯": "match"})
        self.assertEqual((written, put), (0, []))
        self.assertEqual(problems, ["nid 1: 一杯 is not as the run left it"])
        self.assertEqual(states(anki.array(1))["一杯"], ["match"])

    def test_restore_asks_about_a_word_with_no_answer_under_the_new_rules(self):
        rows = [note(1, counted("一杯", [50, 4])), note(50, word("noun", "杯", [2, 5]))]
        anki, _ = self.run_apply(rows, {"一杯": "dontmatch"})
        written, put, stay, unasked, _ = self.run_restore(anki, {})
        self.assertEqual((written, put, stay), (0, [], []))
        self.assertEqual([w.elem[2] for w in unasked], ["一杯"])

    def test_revert_leaves_an_array_that_changed_since(self):
        rows = [note(1, counted("三階", [50, 5])), note(50, word("noun", "階", [2, 5]))]
        anki, _ = self.run_apply(rows, {"三階": "dontmatch"})
        anki.update_note_fields(1, {unlink.ARRAY_FIELD: array(word("noun", "別", [4, 5]))})
        reverted, refused = unlink.revert(anki, self.undo)
        self.assertEqual(reverted, 0)
        self.assertEqual(refused, ["nid 1: the word array changed since, left as is"])
        self.assertEqual(len(self.undo.read_text("utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
