"""What array_slot_repair writes into a word list field, and what it refuses to touch.

The repair writes whole fields into the user's collection, so the edges pinned here are the
ones where it could reach further than the lost slots: a field that parses, a field broken some
other way, a raw text that happens to hold the same characters as a lost slot. Nothing here
talks to Anki.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import array_slot_repair as asr  # noqa: E402

# As the failed run of 2026-09-22 left one: the compound and one sub-word lost their slots
DAMAGED = """[
  ["ソフトクリーム", "noun", "ソフトクリーム", "そふとくりーむ", , [
    ["ソフト", "noun", "ソフト", "そふと", [1790069339347, 2], []],
    ["クリーム", "noun", "クリーム", "くりーむ", , []]
  ]],
  ["を", "particle", "を", "を", ["dontmatch"], []],
  [" 下[くだ]さい", "verb", "下さい", "ください", [1788620065333, 5], []],
  ["。"]
]"""


def test_every_lost_slot_goes_back_to_match_and_nothing_else_changes():
    fix = asr.repair(DAMAGED)
    assert fix.slots == 2 and not fix.why
    arr = json.loads(fix.text)
    assert arr[0][4] == ["match"]
    assert arr[0][5][1][4] == ["match"]
    assert arr[0][5][0][4] == [1790069339347, 2]
    assert arr[1][4] == ["dontmatch"]
    assert arr[2][4] == [1788620065333, 5]
    assert arr[3] == ["。"]


def test_the_repair_is_written_as_format_word_array_writes_a_field():
    fix = asr.repair(DAMAGED)
    assert fix.text == asr.match_flags.format_word_array(json.loads(fix.text))


def test_a_field_that_parses_is_left_alone():
    fix = asr.repair(asr.repair(DAMAGED).text)
    assert fix.text is None and fix.slots == 0 and fix.why == ""


def test_an_empty_field_is_left_alone():
    assert asr.repair("") == asr.Repair(None, 0, "")


def test_a_field_broken_some_other_way_is_listed_not_written():
    fix = asr.repair(DAMAGED.replace('"を", ["dontmatch"]', '"を" ["dontmatch"]'))
    assert fix.text is None and "broken otherwise" in fix.why
    fix = asr.repair('[\n  ["を", "particle", "を", "を", ["dontmatch"], []]\n]]')
    assert fix.text is None and "no empty slot" in fix.why


def test_a_hand_edited_field_is_read_as_the_editor_left_it():
    edited = DAMAGED.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
    fix = asr.repair(edited)
    assert fix.slots == 2 and json.loads(fix.text)[0][4] == ["match"]


def test_a_raw_text_holding_the_same_characters_is_never_filled():
    # A quote inside a raw text is escaped, so `\\", , [` there is text, not a lost slot
    raw = 'a\\", , [b'
    field = DAMAGED.replace('" 下[くだ]さい"', f'"{raw}"')
    fix = asr.repair(field)
    assert fix.slots == 2
    assert json.loads(fix.text)[2][0] == 'a", , [b'
