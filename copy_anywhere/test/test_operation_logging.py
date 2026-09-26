"""Characterization tests for the operation log: one file per triggered copy.

`copy_anywhere/logging_setup.py` has the design in its docstring; what is pinned here is the
part a reader of the file cares about. A run writes exactly one file, whoever inside it does
the logging -- this addon or `jp_text_processing`, whose lines are the ones worth having when
a furigana process produces the wrong reading. Every line carries the definition and the note
it belongs to. A run that had nothing to say leaves no file at all.

`copy_fields`'s `CollectionOp` is driven inline the way `test_copy_fields_op.py` drives it,
except that here `on_success` is called too: it is where the operation's file is closed, and
a file still open is a file that cannot be read back.
"""

import logging

import pytest
from aqt import mw

import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere import logging_setup
from copy_anywhere.logic import copy_fields as copy_fields_module
from copy_anywhere.logic.copy_fields import copy_fields, operation_log_name

# A two-kanji compound read with onyomi: `kana_highlight` logs its way through the reading
# match, so a definition carrying this process is what puts `jp_text_processing`'s lines in
# the same file as the addon's.
SENTENCE = " 会話[かいわ]をする"


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


def highlighting_definition(name: str = "furigana"):
    """A definition whose one field-to-field def runs the kana highlight process."""
    return d.within_note(
        name,
        field_to_field_defs=[
            d.field_to_field(
                "Note",
                copy_from_text="{{Reading}}",
                process_chain=[kana_process()],
            )
        ],
    )


def failing_definition(name: str = "broken"):
    """A definition that logs an error: the field it copies into is not on the note type."""
    return d.within_note(
        name,
        field_to_field_defs=[d.field_to_field("Nope", copy_from_text="x")],
    )


@pytest.fixture
def set_log_level(stub_mw):
    def apply(level: str) -> None:
        stub_mw.addonManager.configs["copy_anywhere"]["log_level"] = level

    return apply


@pytest.fixture
def run_copy_fields(monkeypatch):
    """Run `copy_fields` inline, `on_success` included, and hand back the results."""
    captured: dict = {}

    class InlineCollectionOp:
        def __init__(self, parent, op):
            captured["op"] = op

        def success(self, callback):
            captured["success"] = callback
            return self

        def failure(self, callback):
            captured["failure"] = callback
            return self

        def run_in_background(self, **kwargs):
            results = captured["op"](mw.col)
            captured["success"](results)
            return results

    monkeypatch.setattr(copy_fields_module, "CollectionOp", InlineCollectionOp)
    # `on_success` hands the finished file to the desktop; a test is interested in the path,
    # not in whether Windows can open a .log. Its tooltip is a real Qt window needing a real
    # main window, which is exactly what this suite does not have.
    opened: list[str] = []
    monkeypatch.setattr(copy_fields_module, "open_log_file", opened.append)
    monkeypatch.setattr(copy_fields_module, "tooltip", lambda text, **kwargs: None)

    def run(**kwargs):
        return copy_fields(**kwargs)

    run.opened = opened  # type: ignore[attr-defined]
    return run


def written_log(directory):
    """The one log file the run wrote, and its text."""
    [path] = list(directory.glob("*.log"))
    return path, path.read_text(encoding="utf-8")


class TestOneFilePerOperation:
    def test_a_run_over_two_definitions_writes_one_file_named_after_the_count(
        self, col, run_copy_fields, set_log_level, _operation_logs_go_to_tmp
    ):
        set_log_level("debug")
        note = real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": SENTENCE})

        run_copy_fields(
            copy_definitions=[highlighting_definition("first"), highlighting_definition("second")],
            note_ids_per_definition=[[note.id], [note.id]],
        )

        path, _ = written_log(_operation_logs_go_to_tmp)
        assert path.name.startswith("copy_fields_2_definitions_")

    def test_one_definition_is_named_in_the_filename_instead_of_counted(
        self, col, run_copy_fields, set_log_level, _operation_logs_go_to_tmp
    ):
        set_log_level("debug")
        note = real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": SENTENCE})

        run_copy_fields(
            copy_definitions=[highlighting_definition("furigana")],
            note_ids=[note.id],
        )

        path, _ = written_log(_operation_logs_go_to_tmp)
        assert path.name.startswith("copy_fields_furigana_")

    def test_the_addons_lines_and_jp_text_processings_land_in_the_same_file(
        self, col, run_copy_fields, set_log_level, _operation_logs_go_to_tmp
    ):
        # The whole reason the operation attaches its handler to both loggers: a copy
        # definition's story includes what the furigana processing did inside it.
        set_log_level("debug")
        note = real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": SENTENCE})

        run_copy_fields(
            copy_definitions=[highlighting_definition()],
            note_ids=[note.id],
        )

        _, text = written_log(_operation_logs_go_to_tmp)
        # The wrapper's own line, and one from inside `kana_highlight` two packages down: the
        # alignment search's entry line, which every search logs whatever it goes on to find
        # (the line it logged on success was dropped when the search was rewritten).
        assert "kanji_to_highlight result:" in text
        assert "find_first_complete_alignment - searching splits for word '会話'" in text

    def test_every_line_carries_the_definition_and_the_note_it_is_about(
        self, col, run_copy_fields, set_log_level, _operation_logs_go_to_tmp
    ):
        set_log_level("debug")
        note = real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": SENTENCE})

        run_copy_fields(copy_definitions=[highlighting_definition()], note_ids=[note.id])

        _, text = written_log(_operation_logs_go_to_tmp)
        prefix = f"[furigana][NID:{note.id}]"
        lines = [line for line in text.splitlines() if " DEBUG " in line]
        assert lines
        assert all(prefix in line for line in lines)

    def test_a_second_definitions_lines_carry_its_own_name(
        self, col, run_copy_fields, set_log_level, _operation_logs_go_to_tmp
    ):
        # The prefix is reset per definition, so the second one's lines cannot inherit the
        # first one's name -- which is the only thing telling them apart in one file.
        set_log_level("debug")
        note = real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": SENTENCE})

        run_copy_fields(
            copy_definitions=[highlighting_definition("first"), highlighting_definition("second")],
            note_ids_per_definition=[[note.id], [note.id]],
        )

        _, text = written_log(_operation_logs_go_to_tmp)
        assert "[first]" in text
        assert "[second]" in text


class TestWhenThereIsNoFile:
    def test_a_clean_run_at_the_default_level_leaves_no_file_behind(
        self, col, run_copy_fields, _operation_logs_go_to_tmp
    ):
        # `delay=True` on the handler: the default level is `error`, and a run that reports
        # no error should not litter the logs folder with an empty file.
        note = real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": SENTENCE})

        run_copy_fields(copy_definitions=[highlighting_definition()], note_ids=[note.id])

        assert list(_operation_logs_go_to_tmp.glob("*.log")) == []
        assert run_copy_fields.opened == []

    def test_an_error_at_the_default_level_writes_the_file_and_opens_it(
        self, col, run_copy_fields, _operation_logs_go_to_tmp
    ):
        note = real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": SENTENCE})

        run_copy_fields(copy_definitions=[failing_definition()], note_ids=[note.id])

        path, text = written_log(_operation_logs_go_to_tmp)
        assert "Field 'Nope' not found in note" in text
        assert run_copy_fields.opened == [str(path)]

    def test_a_sync_run_writes_the_file_but_opens_nothing(
        self, col, run_copy_fields, _operation_logs_go_to_tmp
    ):
        # No definitions is the shortest route to a logged error that a sync run reaches:
        # a sync only looks at cards flagged `fc=0`, so a definition over an ordinary note
        # finds nothing to complain about.
        run_copy_fields(
            copy_definitions=[],
            update_sync_result=lambda text, count: None,
        )

        _, text = written_log(_operation_logs_go_to_tmp)
        assert "No definitions given" in text
        assert run_copy_fields.opened == []


class TestTheReferenceCount:
    """`start_operation_log` / `finish_operation_log` around an asynchronous operation.

    The unfocus hook runs its own definitions and then calls `copy_fields`, whose work
    outlives the hook. A scoped "am I inside an operation" flag would close the file when the
    hook returned; the count is what keeps it open until the operation that took the second
    reference is finished with it.
    """

    def test_an_inner_start_joins_the_file_the_outer_one_opened(
        self, _operation_logs_go_to_tmp
    ):
        outer = logging_setup.start_operation_log("outer", "debug")
        inner = logging_setup.start_operation_log("inner", "debug")
        try:
            assert inner == outer
            assert list(_operation_logs_go_to_tmp.glob("*.log")) == []
        finally:
            logging_setup.finish_operation_log()
            logging_setup.finish_operation_log()

    def test_the_file_stays_open_until_the_last_reference_goes(
        self, _operation_logs_go_to_tmp
    ):
        logger = logging.getLogger(logging_setup.ADDON_MODULE)
        logging_setup.start_operation_log("outer", "debug")
        logging_setup.start_operation_log("inner", "debug")
        try:
            # The hook returning is the inner release; the operation is still to come.
            assert logging_setup.finish_operation_log() is None
            logger.error("written after the inner release")
        finally:
            path = logging_setup.finish_operation_log()

        assert path is not None
        _, text = written_log(_operation_logs_go_to_tmp)
        assert "written after the inner release" in text

    def test_the_levels_the_operation_set_are_put_back_when_it_ends(
        self, _operation_logs_go_to_tmp
    ):
        logger = logging.getLogger(logging_setup.ADDON_MODULE)
        before = logger.level
        logging_setup.start_operation_log("outer", "debug")
        assert logger.level == logging.DEBUG
        logging_setup.finish_operation_log()
        assert logger.level == before


class TestTheFileName:
    @pytest.mark.parametrize(
        "definitions, expected",
        [
            ([], "copy_fields_0_definitions"),
            ([d.within_note("one")], "copy_fields_one"),
            ([d.within_note("a"), d.within_note("b")], "copy_fields_2_definitions"),
        ],
        ids=["none", "one", "several"],
    )
    def test_the_name_says_what_triggered_the_run(self, definitions, expected):
        assert operation_log_name(definitions) == expected

    def test_a_definition_name_that_is_not_a_filename_is_made_into_one(
        self, col, run_copy_fields, set_log_level, _operation_logs_go_to_tmp
    ):
        # Definition names are free text and routinely hold `/`, `:` and spaces.
        set_log_level("debug")
        note = real_anki.add_note(col, VOCAB, {"Word": "会", "Reading": SENTENCE})

        run_copy_fields(
            copy_definitions=[highlighting_definition("JP: vocab/kanji")],
            note_ids=[note.id],
        )

        path, _ = written_log(_operation_logs_go_to_tmp)
        assert path.name.startswith("copy_fields_JP_vocab_kanji_")
