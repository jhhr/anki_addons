"""Characterization tests for `apply_process_chain` and the five processes it dispatches to.

The chain is the only place a copy definition transforms a value rather than moving it, and
it is the only place a single misconfigured step can abort a whole run: `FatalProcessError`
from `Fonts check` becomes a `None` return, which the caller reads as "stop everything".
These tests pin what each process actually does with the inputs real definitions give it,
and -- just as important -- which failures are logged-and-survivable and which are fatal.
"""

import json

import pytest

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import (
    apply_process_chain,
    copy_for_single_trigger_note,
)

FONTS_FILE = "fonts.json"

FONTS_DICT = {
    "a": ["FontA.ttf", "FontB.ttf", "FontC.ttf"],
    "b": ["FontB.ttf", "FontC.ttf"],
    "z": ["FontZ.ttf"],
    "all_fonts": ["FontA.ttf", "FontB.ttf", "FontC.ttf", "FontZ.ttf"],
}


def run_chain(chain, text, notes=None, dest_note=None, **kwargs):
    return apply_process_chain(
        process_chain=chain,
        text=text,
        notes=notes if notes is not None else [],
        dest_note=dest_note,
        **kwargs,
    )


def kana_process(**overrides):
    process = {
        "guid": "kana",
        "name": "Kana Highlight",
        "kanji_field": "Word",
        "return_type": "furigana",
        "wrap_readings_in_tags": False,
        "merge_consecutive_tags": False,
        "onyomi_to_katakana": False,
    }
    process.update(overrides)
    return process


def word_process(**overrides):
    process = {"guid": "word", "name": "Word Highlight", "word_field": "Word"}
    process.update(overrides)
    return process


def kanjium_process(**overrides):
    process = {
        "guid": "kanjium",
        "name": "Pitch accent conversion: Kanjium to Javdejong",
        "delimiter": "",
    }
    process.update(overrides)
    return process


# What Yomitan (and Yomichan before it) writes into a field for `{pitch-accents}` from a
# Kanjium pitch dictionary, which only gives a reading and a downstep position. Ported
# from pronunciation-generator.js `createPronunciationText` with pronunciation-style.json
# inlined the way css-style-applier.js does it; nasal and devoice marks are left out
# because Kanjium's data has neither. One inline-block span per mora holds a span per
# character and ends in a "line" span whose style says low, high, or high-then-drop.
_SMALL_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ")
_MORA_STYLE = "display:inline-block;position:relative;"
_DROP_MORA_STYLE = "padding-right:0.1em;margin-right:0.1em;"
_LINE_STYLE = "border-color:currentColor;"
_HIGH_LINE_STYLE = (
    "display:block;user-select:none;pointer-events:none;position:absolute;top:0.1em;"
    "left:0;right:0;height:0;border-top-width:0.1em;border-top-style:solid;"
)
_DROP_LINE_STYLE = (
    "right:-0.1em;height:0.4em;border-right-width:0.1em;border-right-style:solid;"
)


def _is_mora_high(index, position):
    # japanese.js `isMoraPitchHigh`: heiban rises after the first mora, atamadaka is high
    # only on it, and every other position is high from the second mora up to the drop.
    if position == 0:
        return index > 0
    if position == 1:
        return index < 1
    return 0 < index < position


def yomitan_pitch(reading, position):
    morae: list[str] = []
    for char in reading:
        if char in _SMALL_KANA and morae:
            morae[-1] += char
        else:
            morae.append(char)
    html = ""
    for i, mora in enumerate(morae):
        high = _is_mora_high(i, position)
        drop = high and not _is_mora_high(i + 1, position)
        chars = "".join(f'<span style="display:inline;">{char}</span>' for char in mora)
        line = _LINE_STYLE + (_HIGH_LINE_STYLE if high else "")
        line += _DROP_LINE_STYLE if drop else ""
        mora_style = _MORA_STYLE + (_DROP_MORA_STYLE if drop else "")
        html += f'<span style="{mora_style}">{chars}<span style="{line}"></span></span>'
    return f'<span style="display:inline;">{html}</span>'


def yomitan_pitch_list(*pitches):
    # The default `pitch-accent-list` template: more than one pitch becomes an <ol>.
    return "<ol>" + "".join(f"<li>{yomitan_pitch(*pitch)}</li>" for pitch in pitches) + "</ol>"


# The two pieces of javdejong's markup, as his `inline_style` writes them with his default
# config: an overline span, and the closing of it with a downstep notch (U+A71C).
OVER = '<span style="text-decoration:overline;">'
DROP = "</span>&#42780;"


def fonts_of(result):
    """The font list a `Fonts check` result encodes, as a set -- its order is a set's order."""
    return set(json.loads(result))


class TestTheChainItself:
    def test_an_empty_chain_returns_the_text_unchanged(self):
        assert run_chain([], "unchanged") == "unchanged"

    def test_processes_are_applied_in_order(self):
        # Second step consumes the first step's output, so swapping them changes the answer.
        chain = [d.regex_process("a", "b"), d.regex_process("b", "c")]
        assert run_chain(chain, "aaa") == "ccc"

    def test_a_process_with_an_unrecognised_name_is_silently_skipped(self, logger):
        # Every branch is an `elif` on the name with no `else`, so a typo'd or removed
        # process type is a no-op rather than an error.
        chain = [{"guid": "x", "name": "Not A Process"}]
        assert run_chain(chain, "unchanged") == "unchanged"
        assert logger.errors == []

    def test_a_non_fatal_process_error_does_not_stop_the_rest_of_the_chain(self, logger):
        chain = [d.regex_process("(", "X"), d.regex_process("a", "b")]
        assert run_chain(chain, "aaa") == "bbb"
        assert logger.has_error("unterminated subpattern")


class TestRegexReplace:
    def test_numbered_groups_are_substituted(self):
        chain = [d.regex_process(r"<i>(.*)</i>", r"\1")]
        assert run_chain(chain, "<i>abc123</i>def456") == "abc123def456"

    def test_named_groups_are_substituted(self):
        chain = [d.regex_process(r"<i>(?P<content>.*)</i>", r"\g<content>")]
        assert run_chain(chain, "<i>abc123</i>def456") == "abc123def456"

    def test_a_non_matching_regex_leaves_the_text_alone(self, logger):
        chain = [d.regex_process(r"<i>(?P<content>.*)</i>", r"\g<content>")]
        assert run_chain(chain, "<b>abc123</b>def456") == "<b>abc123</b>def456"
        assert logger.errors == []

    def test_an_invalid_regex_is_logged_and_leaves_the_text_alone(self, logger):
        assert run_chain([d.regex_process("(", "X")], "abc") == "abc"
        assert logger.has_error("Error in basic_regex_process")
        assert logger.has_error("missing ), unterminated subpattern")

    def test_a_replacement_naming_a_group_that_does_not_exist_is_logged_and_survivable(
        self, logger
    ):
        # The compile succeeds and the `sub` raises: a second try/except catches it, so this
        # is a different code path from an invalid pattern even though the outcome matches.
        assert run_chain([d.regex_process("a", r"\9")], "abc") == "abc"
        assert logger.has_error("invalid group reference 9")

    def test_an_empty_regex_is_logged_and_leaves_the_text_alone(self, logger):
        assert run_chain([d.regex_process("", "X")], "abc") == "abc"
        assert logger.has_error("Missing 'regex'")

    def test_a_none_replacement_becomes_an_empty_one_before_the_regex_sees_it(self, logger):
        # `regex_process` has a "Missing 'replacement'" branch, but the dispatcher runs the
        # replacement through `get_field_values_from_notes` first and that turns None into
        # "", so the branch is unreachable from a chain: the match is deleted instead.
        chain = [dict(d.regex_process("a", "X"), replacement=None)]
        assert run_chain(chain, "abc") == "bc"
        assert logger.has_error("'copy_from_text' was missing")
        assert not logger.has_error("Missing 'replacement'")

    def test_an_empty_replacement_deletes_the_match(self, logger):
        assert run_chain([d.regex_process("b", "")], "abc") == "ac"
        assert logger.errors == []


class TestRegexFlags:
    def test_no_flags_means_a_case_sensitive_match(self):
        chain = [d.regex_process("abc", "X", flags="")]
        assert run_chain(chain, "ABC abc") == "ABC X"

    def test_the_ascii_flag_restricts_word_characters_to_ascii(self):
        chain = [d.regex_process(r"\w+", "X", flags="ASCII")]
        assert run_chain(chain, "ABC é abc") == "X é X"

    def test_the_dotall_flag_lets_a_dot_cross_a_newline(self):
        chain = [d.regex_process("a.b", "X", flags="DOTALL")]
        assert run_chain(chain, "a\nb") == "X"

    def test_without_dotall_a_dot_does_not_cross_a_newline(self):
        chain = [d.regex_process("a.b", "X", flags="")]
        assert run_chain(chain, "a\nb") == "a\nb"

    def test_several_flags_are_split_on_comma_space_and_ored_together(self):
        chain = [d.regex_process("a.b", "X", flags="IGNORECASE, DOTALL")]
        assert run_chain(chain, "A\nB") == "X"

    def test_a_bogus_flag_name_raises_attribute_error_out_of_the_chain(self):
        # `getattr(re, name)` is not guarded, and `apply_process_chain` only catches
        # FatalProcessError, so this escapes all the way to the caller.
        with pytest.raises(AttributeError, match="NOTAFLAG"):
            run_chain([d.regex_process("a", "X", flags="NOTAFLAG")], "abc")

    def test_flags_separated_without_a_space_are_read_as_one_bogus_name(self):
        # The split is on the literal ", ", so the tidier-looking "A,B" does not work.
        with pytest.raises(AttributeError, match="IGNORECASE,DOTALL"):
            run_chain([d.regex_process("a", "X", flags="IGNORECASE,DOTALL")], "abc")


class TestRegexUseAllNotes:
    @pytest.fixture
    def notes(self, col):
        return [
            real_anki.add_note(col, VOCAB, {"Word": "one", "Meaning": "1"}),
            real_anki.add_note(col, VOCAB, {"Word": "two", "Meaning": "2"}),
        ]

    @pytest.fixture
    def dest(self, col):
        return real_anki.add_note(col, VOCAB, {"Word": "dest", "Meaning": "D"})

    def _process(self, **overrides):
        return d.regex_process(
            "{{Word}}",
            "[{{Meaning}}]",
            regex_separator="|",
            replacement_separator="+",
            use_all_notes=True,
            **overrides,
        )

    def test_the_regex_and_the_replacement_are_both_interpolated_over_every_note(
        self, notes, dest
    ):
        # regex becomes "one|two" and the replacement "[1]+[2]", so both separators are
        # doing real work: the regex one builds an alternation, the replacement one a
        # concatenation that is substituted whole for each match.
        result = run_chain(
            [self._process()], "one two dest", notes=notes, dest_note=dest
        )
        assert result == "[1]+[2] [1]+[2] dest"

    def test_use_all_notes_with_exactly_one_note_falls_back_to_the_dest_note(
        self, notes, dest
    ):
        # `use_all_notes and len(notes) > 1`: one source note is not "more than one", so the
        # source note is ignored entirely and the destination note interpolates instead.
        result = run_chain(
            [self._process()], "one two dest", notes=notes[:1], dest_note=dest
        )
        assert result == "one two [D]"

    def test_without_use_all_notes_the_dest_note_interpolates_even_with_many_notes(
        self, notes, dest
    ):
        process = d.regex_process("{{Word}}", "[{{Meaning}}]", use_all_notes=False)
        result = run_chain([process], "one two dest", notes=notes, dest_note=dest)
        assert result == "one two [D]"

    def test_use_all_notes_with_no_notes_at_all_falls_back_to_the_dest_note(
        self, dest
    ):
        result = run_chain([self._process()], "dest", notes=[], dest_note=dest)
        assert result == "[D]"

    def test_a_variable_can_be_interpolated_into_the_regex(self, dest):
        process = d.regex_process("{{Custom_Var}}", "X", use_all_notes=False)
        result = run_chain(
            [process],
            "abc",
            notes=[],
            dest_note=dest,
            variable_values_dict={"Custom_Var": "b"},
        )
        assert result == "aXc"


class TestKanaHighlight:
    """The CopyAnywhere wrapper around `kana_highlight`, not the algorithm.

    The algorithm has its own suite in `anki_shared/jp_text_processing/`; what is pinned
    here is that the wrapper reads the right field off the note, passes the right
    `WithTagsDef`, and handles a missing field the way it does.
    """

    # A two-kanji compound, both read with onyomi, which is what makes the tag options
    # visible in the output.
    SENTENCE = " 会話[かいわ]をする"

    @pytest.fixture
    def kanji_note(self, col):
        return real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": self.SENTENCE})

    def test_furigana_keeps_the_kanji_and_bolds_the_highlighted_one(self, kanji_note):
        result = run_chain([kana_process()], self.SENTENCE, dest_note=kanji_note)
        assert result == "<b> 会[かい]</b> 話[わ]をする"

    def test_furikanji_swaps_the_kanji_and_the_reading(self, kanji_note):
        chain = [kana_process(return_type="furikanji")]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == "<b> かい[会]</b> わ[話]をする"

    def test_kana_only_drops_the_kanji(self, kanji_note):
        chain = [kana_process(return_type="kana_only")]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == " <b>かい</b>わをする"

    def test_wrap_readings_in_tags_marks_each_reading_with_its_type(self, kanji_note):
        chain = [kana_process(kanji_field="", wrap_readings_in_tags=True)]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == "<on> 会[かい]</on><on> 話[わ]</on>をする"

    def test_merge_consecutive_tags_joins_two_readings_of_the_same_type(
        self, kanji_note
    ):
        chain = [
            kana_process(kanji_field="", wrap_readings_in_tags=True, merge_consecutive_tags=True)
        ]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == "<on> 会話[かいわ]</on>をする"

    def test_onyomi_to_katakana_converts_the_onyomi_readings(self, kanji_note):
        chain = [kana_process(onyomi_to_katakana=True)]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == "<b> 会[カイ]</b> 話[ワ]をする"

    def test_an_empty_kanji_field_highlights_nothing_and_logs_nothing(
        self, kanji_note, logger
    ):
        chain = [kana_process(kanji_field="")]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == " 会話[かいわ]をする"
        assert logger.errors == []

    def test_a_kanji_field_not_on_the_note_is_logged_and_the_process_still_runs(
        self, kanji_note, logger
    ):
        chain = [kana_process(kanji_field="NoSuchField")]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == " 会話[かいわ]をする"
        assert logger.has_error("kanji_field 'NoSuchField' not found in note")

    def test_a_kanji_field_that_is_present_but_empty_logs_the_same_not_found_message(
        self, kanji_note, logger
    ):
        # The loop only remembers a field whose value is truthy, so "present but blank" and
        # "not a field at all" are indistinguishable in the log.
        chain = [kana_process(kanji_field="Freq")]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == " 会話[かいわ]をする"
        assert logger.has_error("kanji_field 'Freq' not found in note")

    def test_the_defaults_for_missing_keys_are_kana_only_with_merged_tags(
        self, kanji_note
    ):
        # A process dict written by an older version of the editor has none of the optional
        # keys; the `.get` defaults in the dispatcher decide what it does.
        chain = [{"guid": "kana", "name": "Kana Highlight"}]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == " <on>かいわ</on>をする"

    def test_a_missing_return_type_logs_and_degrades_to_the_plain_kana_filter(
        self, kanji_note, logger
    ):
        # Only a config with an explicitly empty return_type gets here: the editor always
        # saves one of its three types, and a missing key defaults to kana_only. The step is
        # meant to degrade, not abort -- until `kana_filter` got its `return` back it answered
        # None, which the chain's caller reads as "stop the whole run". What it degrades to is
        # Anki's {{kana:}}, not kana_only: no highlight, no tags, and the space before the
        # word goes with its kanji where kana_only keeps it.
        chain = [kana_process(return_type="")]
        result = run_chain(chain, self.SENTENCE, dest_note=kanji_note)
        assert result == "かいわをする"
        assert logger.has_error("Missing 'return_type'")


class TestWordHighlight:
    @pytest.fixture
    def word_note(self, col):
        return real_anki.add_note(col, VOCAB, {"Word": "漢字", "Meaning": "kanji"})

    def test_the_word_named_by_the_field_is_bolded(self, word_note):
        result = run_chain([word_process()], "漢字を読む", dest_note=word_note)
        assert result == "<b>漢字</b>を読む"

    def test_a_word_field_not_on_the_note_is_logged_and_the_process_still_runs(
        self, word_note, logger
    ):
        chain = [word_process(word_field="NoSuchField")]
        assert run_chain(chain, "漢字を読む", dest_note=word_note) == "漢字を読む"
        assert logger.has_error("word_field 'NoSuchField' not found in note")

    def test_a_word_field_that_is_present_but_empty_logs_the_not_found_message(
        self, word_note, logger
    ):
        chain = [word_process(word_field="Freq")]
        assert run_chain(chain, "漢字を読む", dest_note=word_note) == "漢字を読む"
        assert logger.has_error("word_field 'Freq' not found in note")

    def test_an_empty_word_field_highlights_nothing_and_logs_nothing(self, word_note, logger):
        chain = [word_process(word_field="")]
        assert run_chain(chain, "漢字を読む", dest_note=word_note) == "漢字を読む"
        assert logger.errors == []


class TestKanjiumToJavdejong:
    """The "Kanjium format" is the HTML Yomitan writes for a Kanjium pitch dictionary; the
    target is what javdejong's Japanese Pitch Accent add-on writes for the same word.

    The expected strings are his `format_entry` rules applied to Yomitan's reading, and each
    row's comment gives his own output for that word from his NHK database, in the same
    OVER/DROP shorthand, so the parity is visible: only his katakana differs.
    """

    # 箸 はし[1] from Kanjium's accents.txt.
    KANJIUM = yomitan_pitch("はし", 1)

    def test_the_helper_reproduces_yomitans_own_fixture_verbatim(self):
        # Every other test here trusts `yomitan_pitch`, so pin it to real output: ぶちこむ[3]
        # is the second <li> of "pitch-accents" in Yomitan's own test data, copied verbatim
        # from https://github.com/yomidevs/yomitan/blob/master/test/data/anki-note-builder-test-results.json
        high_line = (
            '<span style="border-color:currentColor;display:block;user-select:none;'
            "pointer-events:none;position:absolute;top:0.1em;left:0;right:0;height:0;"
            "border-top-width:0.1em;border-top-style:solid;"
        )
        yomitan = (
            '<span style="display:inline;">'
            '<span style="display:inline-block;position:relative;">'
            '<span style="display:inline;">ぶ</span>'
            '<span style="border-color:currentColor;"></span></span>'
            '<span style="display:inline-block;position:relative;">'
            '<span style="display:inline;">ち</span>' + high_line + '"></span></span>'
            '<span style="display:inline-block;position:relative;padding-right:0.1em;'
            'margin-right:0.1em;"><span style="display:inline;">こ</span>'
            + high_line
            + "right:-0.1em;height:0.4em;border-right-width:0.1em;"
            'border-right-style:solid;"></span></span>'
            '<span style="display:inline-block;position:relative;">'
            '<span style="display:inline;">む</span>'
            '<span style="border-color:currentColor;"></span></span></span>'
        )
        assert yomitan_pitch("ぶちこむ", 3) == yomitan

    def test_text_without_currentcolor_is_returned_untouched(self):
        assert run_chain([kanjium_process()], "<b>plain</b>") == "<b>plain</b>"

    @pytest.mark.parametrize(
        "reading, position, expected",
        [
            # 桜 サクラ: サ{OVER}クラ</span>
            pytest.param("さくら", 0, f"さ{OVER}くら</span>", id="heiban"),
            # 命 イノチ: {OVER}イ{DROP}ノチ
            pytest.param("いのち", 1, f"{OVER}い{DROP}のち", id="atamadaka"),
            # 貴方 アナタ: ア{OVER}ナ{DROP}タ
            pytest.param("あなた", 2, f"あ{OVER}な{DROP}た", id="nakadaka"),
            # 男 オトコ: オ{OVER}トコ{DROP}
            pytest.param("おとこ", 3, f"お{OVER}とこ{DROP}", id="odaka"),
            # 箸 ハシ: {OVER}ハ{DROP}シ
            pytest.param("はし", 1, f"{OVER}は{DROP}し", id="two-mora-atamadaka"),
            # 木 キ: {OVER}キ{DROP}
            pytest.param("き", 1, f"{OVER}き{DROP}", id="one-mora-atamadaka"),
            # 今日 キョー: {OVER}キョ{DROP}ー
            pytest.param("きょう", 1, f"{OVER}きょ{DROP}う", id="contracted-drop-mora"),
            # 日本 ニッポン: ニ{OVER}ッポ{DROP}ン
            pytest.param("にっぽん", 3, f"に{OVER}っぽ{DROP}ん", id="sokuon"),
        ],
    )
    def test_each_accent_type_becomes_the_javdejong_markup_for_the_same_word(
        self, reading, position, expected
    ):
        result = run_chain([kanjium_process()], yomitan_pitch(reading, position))
        assert result == expected

    def test_heiban_and_odaka_differ_only_by_the_trailing_downstep(self):
        # 端 はし[0] and 橋 はし[2] rise the same way; only the drop onto a following
        # particle tells them apart, so the notch is the whole difference.
        # javdejong: 端 ハ{OVER}シ</span> and 橋 ハ{OVER}シ{DROP}
        chain = [kanjium_process()]
        assert run_chain(chain, yomitan_pitch("はし", 0)) == f"は{OVER}し</span>"
        assert run_chain(chain, yomitan_pitch("はし", 2)) == f"は{OVER}し{DROP}"

    def test_a_one_mora_heiban_word_gets_no_markup_at_all(self):
        # Its only mora is low, so nothing opens and nothing may close -- but the mora line
        # still says currentColor, so the conversion does run. javdejong: 蚊 カ
        assert run_chain([kanjium_process()], yomitan_pitch("か", 0)) == "か"

    def test_a_contracted_sound_inside_the_overline_is_overlined_whole(self):
        # きょ is one mora but two character spans, and only the second sits next to the
        # line span that says "high"; the first must not be left out of the overline.
        # javdejong: 東京 ト{OVER}ーキョー</span>
        result = run_chain([kanjium_process()], yomitan_pitch("とうきょう", 0))
        assert result == f"と{OVER}うきょう</span>"

    def test_kana_outside_the_basic_hiragana_and_katakana_blocks_are_kept(self):
        # ヴ sorts after ン, so a [ァ-ン] character class loses it. Kanjium's accents.txt has
        # the line "ラヴ\t\t1"; javdejong's NHK data has no ヴ at all to compare against.
        result = run_chain([kanjium_process()], yomitan_pitch("ラヴ", 1))
        assert result == f"{OVER}ラ{DROP}ヴ"

    def test_only_the_kana_of_the_morae_survive(self):
        # The output is rebuilt from the morae, so anything around the pitch HTML -- a
        # label, a <br> -- is dropped rather than carried through.
        text = "橋: " + yomitan_pitch("はし", 2) + "<br>"
        assert run_chain([kanjium_process()], text) == f"は{OVER}し{DROP}"

    def test_the_delimiter_joins_the_descriptions(self):
        # Each reading between the middle dots is converted on its own, so one reading's
        # overline never runs into the next.
        chain = [kanjium_process(delimiter=" / ")]
        text = yomitan_pitch("はし", 1) + "・" + yomitan_pitch("はし", 2)
        assert run_chain(chain, text) == f"{OVER}は{DROP}し / は{OVER}し{DROP}"

    def test_an_empty_delimiter_defaults_to_the_katakana_middle_dot(self):
        chain = [kanjium_process(delimiter="")]
        text = self.KANJIUM + "・" + self.KANJIUM
        assert run_chain(chain, text) == f"{OVER}は{DROP}し・{OVER}は{DROP}し"

    def test_the_input_is_always_split_on_the_middle_dot_not_on_the_delimiter(self):
        # The split is hard-coded to the katakana middle dot and only the join uses the
        # configured delimiter, so a custom delimiter is an output format, never an input
        # one: the "/" is not part of any mora, so it vanishes and the readings run together.
        chain = [kanjium_process(delimiter="/")]
        text = self.KANJIUM + "/" + self.KANJIUM
        assert run_chain(chain, text) == f"{OVER}は{DROP}し{OVER}は{DROP}し"

    def test_a_yomitan_multi_pitch_list_no_longer_raises_out_of_the_chain(self):
        # Yomitan lists several pitches as <ol><li>, never with "・", so the whole list is one
        # description. The old regex version, whenever its patterns matched at all, mapped
        # the matches back onto one flat kana list and raised StopIteration here (a drop in
        # the first reading, a later high in the second); the chain only catches
        # FatalProcessError, so that escaped the whole copy.
        # Characterized, not endorsed: each reading's marks are right but the readings run
        # together with nothing between them. Whether <li> should count as a separator is an
        # open question for the user.
        text = yomitan_pitch_list(("きょう", 1), ("こんにち", 0))
        assert run_chain([kanjium_process()], text) == (
            f"{OVER}きょ{DROP}うこ{OVER}んにち</span>"
        )


class TestFontsCheck:
    @pytest.fixture
    def fonts_file(self, media_dir):
        (media_dir / FONTS_FILE).write_text(json.dumps(FONTS_DICT), encoding="utf-8")
        return FONTS_FILE

    def test_the_fonts_valid_for_every_character_come_back_as_a_json_list(
        self, fonts_file
    ):
        result = run_chain([d.fonts_check_process(fonts_file)], "ab")
        assert fonts_of(result) == {"FontB.ttf", "FontC.ttf"}

    def test_limit_to_fonts_intersects_with_the_valid_fonts(self, fonts_file):
        chain = [d.fonts_check_process(fonts_file, limit_to_fonts=["FontC.ttf", "FontZ.ttf"])]
        assert fonts_of(run_chain(chain, "ab")) == {"FontC.ttf"}

    def test_an_empty_limit_to_fonts_intersects_to_nothing(self, fonts_file, logger):
        # `[]` is not None, so it is applied as a limit rather than ignored, and the message
        # blames the characters rather than the limit that actually emptied the set.
        chain = [d.fonts_check_process(fonts_file, limit_to_fonts=[])]
        assert run_chain(chain, "ab") == ""
        assert logger.has_error("no fonts were valid for every character")

    def test_no_font_valid_for_every_character_returns_empty(self, fonts_file, logger):
        assert run_chain([d.fonts_check_process(fonts_file)], "az") == ""
        assert logger.has_error("az - Some characters had valid fonts but no fonts were valid")

    def test_a_single_character_with_no_valid_font_gets_its_own_message(
        self, media_dir, logger
    ):
        # The single-character wording is only reachable when the character IS in the dict
        # but its font list is empty -- a one-character text that misses the dict entirely
        # takes the "no characters had a match" branch instead.
        (media_dir / FONTS_FILE).write_text(json.dumps({"a": []}), encoding="utf-8")
        assert run_chain([d.fonts_check_process(FONTS_FILE)], "a") == ""
        assert logger.has_error("a - No fonts were valid for this character")

    def test_no_character_found_in_the_dictionary_returns_empty(self, fonts_file, logger):
        assert run_chain([d.fonts_check_process(fonts_file)], "qq") == ""
        assert logger.has_error("No characters had a match in the fonts dictionary")

    def test_one_character_missing_from_the_dictionary_is_simply_ignored(
        self, fonts_file
    ):
        # A character with no entry does not narrow the set and does not complain: only a
        # text where NO character is found is treated as a problem.
        result = run_chain([d.fonts_check_process(fonts_file)], "aq")
        assert fonts_of(result) == {"FontA.ttf", "FontB.ttf", "FontC.ttf"}

    def test_the_character_limit_regex_fullmatches_each_character(self, fonts_file):
        # "1" is excluded, so only "a" and "b" narrow the set.
        result = run_chain(
            [d.fonts_check_process(fonts_file, character_limit_regex=r"[a-z]")], "a1b"
        )
        assert fonts_of(result) == {"FontB.ttf", "FontC.ttf"}

    def test_every_character_excluded_falls_back_to_limit_to_fonts(self, fonts_file, logger):
        chain = [
            d.fonts_check_process(
                fonts_file, limit_to_fonts=["FontA.ttf"], character_limit_regex=r"[0-9]"
            )
        ]
        assert fonts_of(run_chain(chain, "ab")) == {"FontA.ttf"}
        assert logger.has_error("ab - All characters excluded by regex")

    def test_every_character_excluded_with_no_limit_falls_back_to_all_fonts(
        self, fonts_file
    ):
        chain = [d.fonts_check_process(fonts_file, character_limit_regex=r"[0-9]")]
        assert fonts_of(run_chain(chain, "ab")) == set(FONTS_DICT["all_fonts"])

    def test_every_character_excluded_with_no_limit_and_no_all_fonts_key_returns_empty(
        self, media_dir, logger
    ):
        (media_dir / FONTS_FILE).write_text(json.dumps({"a": ["FontA.ttf"]}), encoding="utf-8")
        chain = [d.fonts_check_process(FONTS_FILE, character_limit_regex=r"[0-9]")]
        assert run_chain(chain, "ab") == ""
        assert logger.has_error("does not contain an 'all_fonts' key")

    def test_an_empty_limit_to_fonts_on_the_excluded_path_yields_a_list_of_one_empty_name(
        self, fonts_file
    ):
        # The result is built by string interpolation rather than json.dumps, so joining an
        # empty list produces '[""]' -- a one-element list holding an empty font name.
        chain = [
            d.fonts_check_process(fonts_file, limit_to_fonts=[], character_limit_regex=r"[0-9]")
        ]
        assert run_chain(chain, "ab") == '[""]'

    def test_empty_text_is_logged_and_returns_empty(self, fonts_file, logger):
        assert run_chain([d.fonts_check_process(fonts_file)], "") == ""
        assert logger.has_error("Text was empty")

    def test_a_missing_fonts_dict_file_name_returns_empty_without_touching_the_disk(
        self, logger
    ):
        assert run_chain([d.fonts_check_process("")], "ab") == ""
        assert logger.has_error("Missing 'fonts_dict_file'")


class TestFatalProcessError:
    def test_a_missing_file_returns_none_from_the_chain(self, col, logger):
        chain = [d.fonts_check_process("does_not_exist.json")]
        assert run_chain(chain, "ab") is None
        assert logger.has_error("Error in Fonts check process")
        assert logger.has_error("does not exist")

    def test_an_empty_file_returns_none_from_the_chain(self, media_dir, logger):
        (media_dir / FONTS_FILE).write_text("   \n", encoding="utf-8")
        assert run_chain([d.fonts_check_process(FONTS_FILE)], "ab") is None
        assert logger.has_error("is empty")

    def test_invalid_json_returns_none_from_the_chain(self, media_dir, logger):
        (media_dir / FONTS_FILE).write_text("{not json", encoding="utf-8")
        assert run_chain([d.fonts_check_process(FONTS_FILE)], "ab") is None
        assert logger.has_error("Error parsing JSON in file")

    def test_the_file_is_read_before_the_empty_text_check_so_empty_text_still_aborts(
        self, col
    ):
        # Order matters: a definition whose source value happens to be blank still dies on a
        # broken fonts file rather than quietly producing "".
        chain = [d.fonts_check_process("does_not_exist.json")]
        assert run_chain(chain, "") is None

    def test_later_processes_in_the_chain_do_not_run(self, col):
        chain = [d.fonts_check_process("does_not_exist.json"), d.regex_process("a", "b")]
        assert run_chain(chain, "aaa") is None

    def test_the_caller_turns_the_none_into_a_failed_definition(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
        definition = d.within_note(
            field_to_field_defs=[
                d.field_to_field("Reading", "{{Word}}"),
                d.field_to_field(
                    "Note",
                    "{{Word}}",
                    process_chain=[d.fonts_check_process("does_not_exist.json")],
                ),
                d.field_to_field("Freq", "999"),
            ],
            add_tags="processed",
        )
        copied_into_notes: list = []

        succeeded = copy_for_single_trigger_note(
            definition, note, copied_into_notes=copied_into_notes
        )

        # The abort is whole-definition, not per-field: the write before the failure is in
        # the note object, but the one after it never ran and neither did the tag step below
        # them -- and nothing is handed to the caller to save, so none of it is persisted.
        assert succeeded is False
        assert copied_into_notes == []
        assert note["Reading"] == "neko"
        assert note["Note"] == ""
        assert note["Freq"] == ""
        assert not note.has_tag("processed")
        assert logger.has_error("Process chain failed for field Note")

    def test_a_file_definition_reports_the_filename_rather_than_the_field(self, col, logger):
        note = real_anki.add_note(col, VOCAB, {"Word": "neko"})
        definition = d.within_note(
            field_to_file_defs=[
                d.field_to_file(
                    "out.txt",
                    "{{Word}}",
                    process_chain=[d.fonts_check_process("does_not_exist.json")],
                )
            ],
        )

        succeeded = copy_for_single_trigger_note(definition, note)

        assert succeeded is False
        assert logger.has_error("Process chain failed for file out.txt")


class TestFontsDictFileCache:
    """The `file_cache` dict `copy_fields_in_background` builds once and hands to every note."""

    @pytest.fixture
    def open_counter(self, monkeypatch, media_dir):
        (media_dir / FONTS_FILE).write_text(json.dumps(FONTS_DICT), encoding="utf-8")
        import builtins

        real_open = builtins.open
        counts = {"n": 0}

        def counting_open(file, *args, **kwargs):
            if str(file).endswith(FONTS_FILE):
                counts["n"] += 1
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", counting_open)
        return lambda: counts["n"]

    def test_the_dict_is_read_from_disk_once_across_many_notes(self, open_counter):
        cache: dict = {}
        chain = [d.fonts_check_process(FONTS_FILE)]
        results = [run_chain(chain, "ab", file_cache=cache) for _ in range(5)]

        assert open_counter() == 1
        assert all(fonts_of(result) == {"FontB.ttf", "FontC.ttf"} for result in results)

    def test_the_cache_is_keyed_by_the_configured_file_name(self, open_counter):
        cache: dict = {}
        run_chain([d.fonts_check_process(FONTS_FILE)], "ab", file_cache=cache)
        assert cache == {FONTS_FILE: FONTS_DICT}

    def test_without_a_cache_the_file_is_reread_for_every_call(self, open_counter):
        chain = [d.fonts_check_process(FONTS_FILE)]
        for _ in range(3):
            run_chain(chain, "ab")
        assert open_counter() == 3

    def test_a_cached_dict_is_used_even_after_the_file_on_disk_changes(
        self, open_counter, media_dir
    ):
        # The cache has no invalidation, which is what makes it a per-run cache rather than
        # a global one: a file edited mid-run is not picked up until the next run.
        cache: dict = {}
        chain = [d.fonts_check_process(FONTS_FILE)]
        first = run_chain(chain, "ab", file_cache=cache)
        (media_dir / FONTS_FILE).write_text(json.dumps({"a": ["Other.ttf"]}), encoding="utf-8")
        second = run_chain(chain, "ab", file_cache=cache)

        assert open_counter() == 1
        assert second == first

    def test_a_fatal_read_is_not_cached_so_every_note_retries_it(self, open_counter):
        # Nothing is written to the cache on the failure path, so a broken file is opened
        # again on the next call -- moot in practice, since the first failure aborts the run.
        cache: dict = {}
        chain = [d.fonts_check_process("missing_" + FONTS_FILE)]
        assert run_chain(chain, "ab", file_cache=cache) is None
        assert cache == {}
