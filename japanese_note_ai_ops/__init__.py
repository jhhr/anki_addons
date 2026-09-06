import os
import logging

from anki import hooks
from anki.notes import Note, NoteId
from aqt import gui_hooks
from aqt import mw
from aqt.browser import Browser
from aqt.qt import QAction, qconnect, QMenu

# Put the vendored 'lib' on sys.path - the locally rebuilt tree if there is one, then the
# shipped halves - before anything that imports from it. Nothing below may move above this line.
from .shared.utils.vendor_path import add_vendor_paths, vendor_health  # noqa: E402

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
ADDON_NAME = "Simple Anki AI Prompts"

add_vendor_paths(ADDON_DIR)

# Two string comparisons and a small JSON read, so it runs at every startup - and it has to,
# because Anki's launcher updates Anki's Python independently of any addon, and a lib built for
# the previous one degrades silently rather than raising. It also has to run here, while
# sys.path is as add_vendor_paths just left it and before anything has imported from it.
# Acting on the verdict needs a main window, so that waits for main_window_did_init.
VENDOR_HEALTH = vendor_health(ADDON_DIR)

# E402 - module level import not at top of file
from .shared.utils.vendor_rebuild_ui import install_rebuild_ui  # noqa: E402
from .utils import get_field_config  # noqa: E402
from .call_logging import in_bulk_op, start_call_log  # noqa: E402


from .async_api_ops.clean_meaning import (  # noqa: E402
    clean_meaning_in_note,
    clean_selected_notes,
)
from .async_api_ops.translate_field import (  # noqa: E402
    translate_selected_notes,
    translate_sentence_in_note,
)
from .async_api_ops.make_kanji_story import (  # noqa: E402
    make_stories_for_selected_notes,
    make_story_for_note,
)
from .async_api_ops.kanjify_sentence import (  # noqa: E402
    kanjify_selected_notes,
)
from .async_api_ops.extract_words import (  # noqa: E402
    extract_words_from_selected_notes,
    extract_words_in_note,
    extract_words_test_compare_from_selected_notes,
)
from .async_api_ops.migrate_compound_verbs import (  # noqa: E402
    migrate_compound_verbs_from_selected_notes,
)
from .async_api_ops.match_words_to_notes import (  # noqa: E402
    match_words_to_notes_from_selected,
    match_single_word_to_notes_from_selected,
)

from .async_api_ops.make_all_meanings import (  # noqa: E402
    make_meanings_selected_notes,
    merge_meanings_selected_notes,
)
from .async_api_ops.new_note_all_ops import (  # noqa: E402
    new_note_all_ops_selected_notes,
)
from .sync_local_ops.find_missing_matched_note_ids import (  # noqa: E402
    find_missing_matched_note_ids_selected_notes,
)
from .sync_local_ops.tag_notes_matched_status import (  # noqa: E402
    tag_notes_matched_status_from_selected,
)
from .sync_local_ops.deduplicate_existing_meaning_notes import (  # noqa: E402
    deduplicate_existing_meaning_notes_selected_notes,
)
from .sync_local_ops.make_fine_tuning_data import (  # noqa: E402
    make_kanjify_sentence_fine_tuning_data,
    make_extract_words_fine_tuning_data,
)


# Initialize root logger for the addon at module load
def setup_addon_logging():
    """Set up the root logger for this addon"""
    addon_logger = logging.getLogger(__name__.split(".")[0])  # Get root addon logger

    # Set initial level (will be updated from config)
    addon_logger.setLevel(logging.ERROR)

    # Prevent propagation to Anki's loggers
    addon_logger.propagate = False


setup_addon_logging()


# Function to be executed when the browser menus are initialized
def on_browser_will_show_context_menu(browser: Browser, menu: QMenu):
    logger = logging.getLogger(__name__)
    start_call_log("add_note")

    # Create a new action for the context menu
    meaning_action = QAction("Clean dictionary meaning", mw)
    translation_action = QAction("Translate sentence", mw)
    kanji_story_action = QAction("Generate kanji story", mw)
    component_words_action = QAction("Kanjify sentence", mw)
    extract_words_action = QAction("Extract words", mw)
    extract_words_test_compare_action = QAction("Test extract words prompt", mw)
    migrate_compound_verbs_action = QAction("Migrate compound verbs to prefix/suffix verbs", mw)
    match_words_action = QAction("Match extracted words to notes", mw)
    rematch_single_word_action = QAction("Rematch all single word to notes", mw)
    rematch_processed_single_word_action = QAction("Rematch processed single words to notes", mw)
    match_remaining_single_word_action = QAction(
        "Match remaining unprocessed single words to notes", mw
    )
    find_missing_matched_note_ids_action = QAction(
        "Find missing matched note ids for selected notes", mw
    )
    tag_notes_matched_status_action = QAction("Tag notes matched status", mw)
    deduplicate_existing_meaning_notes_action = QAction("Deduplicate existing meaning notes", mw)
    export_kanjify_ft_action = QAction("Export kanjify fine-tuning data", mw)
    export_extract_words_ft_action = QAction("Export extract-words fine-tuning data", mw)
    make_all_meanings_action = QAction("Generate all meanings for selected notes", mw)
    merge_meanings_action = QAction("Merge existing meanings for selected notes", mw)
    new_note_all_ops_action = QAction("Run all ops for new notes", mw)

    # Connect the action to the operation
    selected_nids = browser.selectedNotes()
    qconnect(
        meaning_action.triggered,
        lambda: clean_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        translation_action.triggered,
        lambda: translate_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        kanji_story_action.triggered,
        lambda: make_stories_for_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        component_words_action.triggered,
        lambda: kanjify_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        extract_words_action.triggered,
        lambda: extract_words_from_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        extract_words_test_compare_action.triggered,
        lambda: extract_words_test_compare_from_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        migrate_compound_verbs_action.triggered,
        lambda: migrate_compound_verbs_from_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        match_words_action.triggered,
        lambda: match_words_to_notes_from_selected(selected_nids, parent=browser),
    )
    qconnect(
        rematch_single_word_action.triggered,
        lambda: match_single_word_to_notes_from_selected(
            selected_nids, parent=browser, reprocess_words="both"
        ),
    )
    qconnect(
        rematch_processed_single_word_action.triggered,
        lambda: match_single_word_to_notes_from_selected(
            selected_nids, parent=browser, reprocess_words="only_processed"
        ),
    )
    qconnect(
        match_remaining_single_word_action.triggered,
        lambda: match_single_word_to_notes_from_selected(
            selected_nids, parent=browser, reprocess_words="only_unprocessed"
        ),
    )
    qconnect(
        make_all_meanings_action.triggered,
        lambda: make_meanings_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        merge_meanings_action.triggered,
        lambda: merge_meanings_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        new_note_all_ops_action.triggered,
        lambda: new_note_all_ops_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        find_missing_matched_note_ids_action.triggered,
        lambda: find_missing_matched_note_ids_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        tag_notes_matched_status_action.triggered,
        lambda: tag_notes_matched_status_from_selected(selected_nids, parent=browser),
    )
    qconnect(
        deduplicate_existing_meaning_notes_action.triggered,
        lambda: deduplicate_existing_meaning_notes_selected_notes(selected_nids, parent=browser),
    )
    qconnect(
        export_kanjify_ft_action.triggered,
        lambda: make_kanjify_sentence_fine_tuning_data(selected_nids, parent=browser),
    )
    qconnect(
        export_extract_words_ft_action.triggered,
        lambda: make_extract_words_fine_tuning_data(selected_nids, parent=browser),
    )

    ai_menu = menu.addMenu("AI helper")
    if ai_menu is None:
        logger.error("Error: AI helper menu could not be created.")
        return
    # Add the action to the browser's card context menu

    # Async ops
    ai_menu.addAction(meaning_action)
    ai_menu.addAction(translation_action)
    ai_menu.addAction(kanji_story_action)
    ai_menu.addAction(component_words_action)
    ai_menu.addAction(extract_words_action)
    ai_menu.addAction(extract_words_test_compare_action)
    ai_menu.addAction(migrate_compound_verbs_action)
    ai_menu.addAction(match_words_action)
    ai_menu.addAction(rematch_single_word_action)
    ai_menu.addAction(rematch_processed_single_word_action)
    ai_menu.addAction(match_remaining_single_word_action)
    ai_menu.addAction(make_all_meanings_action)
    ai_menu.addAction(merge_meanings_action)
    ai_menu.addAction(new_note_all_ops_action)
    ai_menu.addSeparator()
    # Sync ops
    ai_menu.addAction(find_missing_matched_note_ids_action)
    ai_menu.addAction(tag_notes_matched_status_action)
    ai_menu.addAction(deduplicate_existing_meaning_notes_action)
    ai_menu.addAction(export_kanjify_ft_action)
    ai_menu.addAction(export_extract_words_ft_action)


def run_op_on_field_unfocus(changed: bool, note: Note, field_idx: int):
    logger = logging.getLogger(__name__)
    # A hook the user drives one field at a time, so the call really is the unit of work and a
    # log file per call is the right granularity - unlike note_will_be_added, which a bulk run
    # fires a thousand times in a row.
    start_call_log("add_note")

    note_type = note.note_type()
    if not note_type:
        return
    note_type_name = note_type["name"]
    config = mw.addonManager.getConfig(__name__)
    if not config:
        logger.error("Error: Missing addon configuration")
        return

    field_name = note_type["flds"][field_idx]["name"]
    cur_field_value = note[field_name]

    if note_type_name == "Kanji draw":
        story_field = get_field_config(config, "story_field", note_type)
        if field_name == story_field and cur_field_value == "":
            return make_story_for_note(config, note, {}, {})

    if note_type_name == "Japanese vocab note":
        translated_sentence_field = get_field_config(config, "translated_sentence_field", note_type)
        if field_name == translated_sentence_field and cur_field_value == "":
            return translate_sentence_in_note(config, note, {}, {})


def run_op_on_add_note(note: Note):
    # The tag check comes before everything else, and that ordering is the whole cost of this
    # hook on a bulk run. `match_words_to_notes` sets this tag on every note it creates, so
    # these are exactly the notes the hook has nothing to do for - and it used to decide that
    # last, after building a log file, closing the previous one, reading the note type and
    # reading the config twice. Measured over one run: 1,512 notes x ~1.0s = 25.8 minutes,
    # 98.9% of the note-adding phase, to conclude there was nothing to do. Nothing above this
    # line may need the note type or the config.
    if note.has_tag("new_matched_jp_word"):
        # Happening within match_words_to_notes, which causes some problems
        return

    logger = logging.getLogger(__name__)
    if not in_bulk_op():
        # A note added by hand, which is the case a log file per call was made for. Inside a
        # bulk op the run owns the handler and the phase it belongs to has already installed
        # one; replacing it per note is what produced 1,453 log files for a single run.
        start_call_log("add_note")

    note_type = note.note_type()
    if not note_type:
        return
    note_type_name = note_type["name"]
    config = mw.addonManager.getConfig(__name__)
    if not config:
        logger.error("Error: Missing addon configuration")
        return

    if note_type_name == "Japanese vocab note":
        notes_to_update_dict: dict[NoteId, Note] = {}
        try:
            clean_meaning_in_note(config, note, {}, notes_to_update_dict)
            extract_words_in_note(config, note, {}, notes_to_update_dict)
        except Exception as e:
            logger.error(
                f"Error in clean_meaning_in_note or extract_words_in_note: {e}", exc_info=True
            )
        if notes_to_update_dict:
            updated_notes = list(notes_to_update_dict.values())
            # Filter out the added note itself from the updated notes
            updated_notes = [n for n in updated_notes if n.id != note.id]
            logger.info(f"Updating {len(updated_notes)} notes after adding new note")
            mw.col.update_notes(updated_notes)


# Register to card adding hook
hooks.note_will_be_added.append(lambda _col, note, _deck_id: run_op_on_add_note(note))

# hooks.note_will_be_added.append(lambda _col, note, _deck_id: translate_sentence_in_note(
# note, config=mw.addonManager.getConfig(__name__)))

# Register to context menu initialization hook
gui_hooks.browser_will_show_context_menu.append(on_browser_will_show_context_menu)

# Register to field unfocus hook
gui_hooks.editor_did_unfocus_field.append(run_op_on_field_unfocus)

# Offer to rebuild the vendored packages when they do not fit this machine, and put the same
# rebuild in the Tools menu for anyone who wants rapidfuzz's compiled half - which the shipped
# lib/ leaves out, because five platforms of it is ~30 MB.
install_rebuild_ui(ADDON_DIR, ADDON_NAME, VENDOR_HEALTH)
