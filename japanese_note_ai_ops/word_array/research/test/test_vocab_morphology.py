"""What `family()` must and must not call a disagreement about a reading.

Every case here is one the collection really holds. The ones that must come back
`NOT_A_QUESTION` were raised to a judging pass that found nothing wrong on either side; the ones
that must keep their family were found by diffing the reports before and after the gates went
in, and each is a repair that would have been lost had a gate been drawn one notch wider.
"""

import sys
from pathlib import Path

import pytest

# `vocab_morphology` is a leaf: it imports `re` and `unicodedata` and nothing of the addon, so
# it needs only its own directory on the path. This is done here rather than in a `conftest.py`
# because pytest imports every `conftest.py` under the bare name `conftest`, and the suite
# already has one that `copy_anywhere/test` imports helpers from by that name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vocab_morphology as m  # noqa: E402

NOT_A_QUESTION = m.NOT_A_QUESTION


# --- pairs that are not a question at all -------------------------------------------------


@pytest.mark.parametrize(
    "note_form, note_reading, link_form, link_reading, depth",
    [
        # Identical readings reached WIDTH because the spellings differ only by width, and
        # WIDTH is in the repair's REPAIRABLE: the pair was queued as a reading to fix.
        ("ＰＫ", "ぴーけー", "PK", "ぴーけー", 0),
        ("ＩＴ", "あいてぃー", "IT", "あいてぃー", 0),
        # The element's own Latin text echoed into the reading slot.
        ("OL", "おーえる", "OL", "OL", 0),
        ("DVD", "でぃーぶいでぃー", "DVD", "DVD", 0),
        ("CD", "しーでぃー", "CD", "CD", 0),
        ("CG", "しーじー", "CG", "CG", 0),
        # The tokenizer's part-of-speech name in the reading slot.
        ("％", "ぱーせんと", "%", "きごう", 0),
        # A reading cut off mid-mora: no reading ends in a bare sokuon.
        ("蹄鉄", "ていてつ", "蹄鉄", "ていてっ", 0),
        # One reading in two notations, where no kana spelling decides which.
        ("手前", "てめえ", "手前", "てめー", 0),
        # The note's own reading with a piece of itself glued on the front.
        ("浮かない顔", "うかないかお", "浮かない顔", "うかないうかないかお", 0),
        ("バブル経済", "ばぶるけいざい", "バブル経済", "ばぶるばぶるけいざい", 0),
        ("箇所", "かしょ", "箇所", "かかしょ", 0),
        # Compound-internal rendaku of the very reading the note stores: 髪型, 膝頭.
        ("型", "かた", "型", "がた", 1),
        ("頭", "かしら", "頭", "がしら", 1),
    ],
)
def test_not_a_question(note_form, note_reading, link_form, link_reading, depth):
    assert m.family(note_form, note_reading, link_form, link_reading, depth) == NOT_A_QUESTION


@pytest.mark.parametrize(
    "form, readings",
    [("笹竹", ("ささたけ", "ささだけ")), ("端っこ", ("はしっこ", "はじっこ")),
     ("教示", ("きょうじ", "きょうし"))],
)
def test_attested_variants_are_not_a_question(form, readings):
    """A dictionary heads the spelling both ways with one meaning, so neither side is wrong."""
    assert m.family(form, readings[0], form, readings[1]) == NOT_A_QUESTION
    assert m.family(form, readings[1], form, readings[0]) == NOT_A_QUESTION


def test_not_a_question_is_not_none():
    """`None` and `NOT_A_QUESTION` must stay distinct answers.

    `vocab_unlink` falls through a `None` to "does another note own this word", and for a
    damaged reading slot that answer is yes for the wrong reason: DVD[DVD] is owned by a note
    that reads DVD as "DVD", so the element would be unlinked from the note that has it right.
    """
    assert m.family("DVD", "でぃーぶいでぃー", "DVD", "DVD", 0) is not None


# --- pairs that must keep their family ----------------------------------------------------


def test_a_kana_spelling_still_decides_its_own_notation():
    """コーラス transliterates to こーらす, so こうらす is a typo and not a second notation.

    Folding long vowels unconditionally swallowed this repair. A kana spelling is the one place
    where which notation is right *is* decidable, so the fold is off there.
    """
    assert m.family("コーラス", "こうらす", "コーラス", "こーらす") == m.TYPO


def test_okurigana_at_depth_is_still_a_word():
    """染みる[じみる] sits where 型[がた] sits, and is not the same thing.

    Both are a sub-element voicing the note's own reading, but じみる is a suffix with its own
    dictionary entry - a split to act on. The okurigana is what tells an inflecting word from a
    bound morpheme, so the compound-internal exemption asks for a form with none.
    """
    assert m.family("染みる", "しみる", "染みる", "じみる", 1) == m.RENDAKU


def test_rendaku_at_depth_zero_is_still_a_question():
    """砂埃 is the case the exemption must not reach: a free-standing word, either side wrong."""
    assert m.family("砂埃", "すなほこり", "砂埃", "すなぼこり", 0) == m.RENDAKU


def test_a_real_reading_for_a_latin_spelling_is_still_a_question():
    """ｎｍ[なのめーとる] against a note reading えぬえむ is a real disagreement.

    The wider rule - "the form holds no Japanese script" - would have silenced this, and the
    judging pass asked for the note to be repaired to なのめーとる.
    """
    assert m.family("ｎｍ", "えぬえむ", "nm", "なのめーとる") == m.WIDTH


def test_a_damaged_latin_note_reading_is_still_a_question():
    """ＩＴ with a `vocab-kana` of "it" is damage the repair fixes from the array's reading."""
    assert m.family("ＩＴ", "it", "IT", "あいてぃー") == m.WIDTH


# --- the pieces --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reading, folded",
    [("てめえ", "てめー"), ("てめー", "てめー"), ("こう", "こー"), ("こー", "こー"),
     ("おおう", "おーう"), ("かう", "かう"), ("いう", "いう")],
)
def test_fold_long_vowels(reading, folded):
    """う after an え or お row kana is a long vowel; あ and い rows spell theirs out."""
    assert m.fold_long_vowels(reading) == folded


def test_fold_does_not_merge_two_words():
    """買う and 言う end in a う that is not a long vowel, so they must survive the fold."""
    assert m.fold_long_vowels("かう") != m.fold_long_vowels("こう")


@pytest.mark.parametrize(
    "form, reading, expected",
    [
        ("OL", "OL", True),
        ("%", "きごう", True),
        ("蹄鉄", "ていてっ", True),
        ("記号", "きごう", False),  # the note really is that word
        ("きごう", "きごう", False),
        ("端っこ", "はじっこ", False),
        ("型", "がた", False),
    ],
)
def test_not_a_reading(form, reading, expected):
    assert m.not_a_reading(form, reading) is expected


def test_not_a_reading_on_an_empty_slot():
    assert m.not_a_reading("何か", "") is True


@pytest.mark.parametrize(
    "note_reading, link_reading, expected",
    [
        ("うかないかお", "うかないうかないかお", True),
        ("かしょ", "かかしょ", True),
        ("ばぶるけいざい", "ばぶるばぶるけいざい", True),
        ("かしょ", "かしょ", False),  # the same reading is not a doubled one
        ("すなほこり", "すなぼこり", False),
        ("かお", "わらいかお", False),  # a longer word ending in the note's is not a doubling
    ],
)
def test_doubled_reading(note_reading, link_reading, expected):
    assert m.doubled_reading(note_reading, link_reading) is expected


# --- the families that were already there -------------------------------------------------


@pytest.mark.parametrize(
    "note_form, note_reading, link_form, link_reading, expected",
    [
        ("行き", "いき", "行く", "いく", "DEVERBAL"),
        ("行く", "いく", "行き", "いき", "VERB_OF_DEVERBAL"),
        ("勉強", "べんきょう", "勉強する", "べんきょうする", "SURU_COMPOUND"),
        ("狡賢い", "ずるがしこい", "狡賢い", "ずるかしこい", "RENDAKU"),
        ("七", "しち", "七番組寮", "しちばんぐみりょう", "PHRASE_AROUND"),
    ],
)
def test_existing_families_are_unchanged(
    note_form, note_reading, link_form, link_reading, expected
):
    """The gates run first, so they must not have swallowed any family that was working."""
    assert m.family(note_form, note_reading, link_form, link_reading) == getattr(m, expected)
