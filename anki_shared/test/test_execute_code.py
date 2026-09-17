from anki_shared.interpolate.execute_code import execute_code_core, execute_code_for_field


class FakeNote:
    """Minimal note stub: execute_code only reads fields and cards off it."""

    id = 1

    def __init__(self, fields: dict):
        self._fields = fields

    def __getitem__(self, key: str) -> str:
        return self._fields[key]

    def keys(self):
        return self._fields.keys()

    def cards(self):
        return []

    def note_type(self):
        return {"type": 0, "tmpls": [{"name": "Card 1"}]}


class TestExecuteCodeCore:
    def test_returns_result(self):
        result, error = execute_code_core("return 1 + 1", FakeNote({}))
        assert error is None
        assert result == 2

    def test_reads_note_fields(self):
        note = FakeNote({"Word": "猫"})
        result, error = execute_code_core("return note['Word']", note)
        assert error is None
        assert result == "猫"

    def test_syntax_error_is_reported_with_user_line_number(self):
        result, error = execute_code_core("return (", FakeNote({}))
        assert result is None
        assert error is not None
        assert "line 1" in error

    def test_empty_code_is_a_no_op(self):
        assert execute_code_core("   ", FakeNote({})) == (None, None)


class TestExtraGlobals:
    """Extra names let a caller expose context beyond the note, which is how
    other addons reuse this sandbox (e.g. the card currently being reviewed)."""

    def test_extra_name_is_available(self):
        result, error = execute_code_core(
            "return card_ivl * 2", FakeNote({}), extra_globals={"card_ivl": 21}
        )
        assert error is None
        assert result == 42

    def test_extra_names_may_replace_a_standard_name(self):
        result, error = execute_code_core(
            "return note", FakeNote({}), extra_globals={"note": "replaced"}
        )
        assert error is None
        assert result == "replaced"

    def test_absent_without_extra_globals(self):
        result, error = execute_code_core("return card_ivl", FakeNote({}))
        assert result is None
        assert error is not None
        assert "card_ivl" in error

    def test_passed_through_by_execute_code_for_field(self):
        result, error = execute_code_for_field(
            "return card_ivl", FakeNote({}), extra_globals={"card_ivl": 21}
        )
        assert error is None
        assert result == "21"


class TestJapaneseGlobals:
    """The reading processor and the word array codecs, for definition code that has to
    render a sentence the way the rest of the pipeline wrote it."""

    SENTENCE = " 猫[ねこ]が 屋根[やね]に 上[のぼ]る"

    def test_kana_highlight_runs_on_a_field(self):
        result, error = execute_code_core(
            'return kana_highlight(None, note["Sentence"], "furigana")',
            FakeNote({"Sentence": self.SENTENCE}),
        )
        assert error is None
        assert result == (
            "<kun> 猫[ねこ]</kun>が<kun> 屋根[やね]</kun>に"
            "<kun> 上[のぼ]</kun><oku>る</oku>"
        )

    def test_the_kana_highlight_step_s_settings_are_the_arguments(self):
        """Named as the step names them, so code that has to match a field the step wrote
        can be written from looking at the step. Off, a compound's reading is split over
        its kanji, which is how the sentence fields in this collection are written."""
        result, error = execute_code_core(
            "return kana_highlight("
            '    None, note["Sentence"], "furigana", merge_consecutive_tags=False)',
            FakeNote({"Sentence": self.SENTENCE}),
        )
        assert error is None
        assert "<kun> 屋[や]</kun><kun> 根[ね]</kun>" in result

    def test_a_hand_edited_word_array_field_still_decodes(self):
        """json.loads would not: Anki's editor turns the rows into <br> and &nbsp;."""
        mangled = '[<br>&nbsp; ["猫", "noun", "猫", "ねこ", [], []]<br>]'
        result, error = execute_code_core(
            'return decode_word_array(note["Words"])', FakeNote({"Words": mangled})
        )
        assert error is None
        assert result == [["猫", "noun", "猫", "ねこ", [], []]]

    def test_format_word_array_writes_one_word_per_row(self):
        result, error = execute_code_core(
            'return format_word_array([["猫"], ["が"]])', FakeNote({})
        )
        assert error is None
        assert result == '[\n  ["猫"],\n  ["が"]\n]'
