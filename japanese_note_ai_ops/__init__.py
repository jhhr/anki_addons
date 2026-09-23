import os
import logging
from typing import Optional

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
ADDON_NAME = "Japanese Note AI Ops"

add_vendor_paths(ADDON_DIR)

# Two string comparisons and a small JSON read, so it runs at every startup - and it has to,
# because Anki's launcher updates Anki's Python independently of any addon, and a lib built for
# the previous one degrades silently rather than raising. It also has to run here, while
# sys.path is as add_vendor_paths just left it and before anything has imported from it.
# Acting on the verdict needs a main window, so that waits for main_window_did_init.
VENDOR_HEALTH = vendor_health(ADDON_DIR)

# E402 - module level import not at top of file
from .shared.utils.vendor_rebuild_ui import install_rebuild_ui  # noqa: E402

# A vendored package that is *missing* rather than merely stale raises here - a fresh checkout
# whose gitignored lib/ never arrived, or an update that added a requirement the shipped tree
# predates. An addon that dies at import time never reaches main_window_did_init, and that is
# where the rebuild is offered *and* where the Tools action that repairs it by hand is added:
# dying here takes both repairs down with it, so the one failure the vendoring exists to fix is
# the one it could not survive. These imports degrade instead. The addon loads without its
# operations, and the offer at the bottom of this file is what puts them back.
#
# Only ImportError is caught, and it is not swallowed: it sets MISSING_PACKAGE, which the health
# verdict below then names out loud. Anything else an addon module raises is a real bug and must
# reach Anki's error report exactly as it always did.
try:
    from .utils import get_field_config  # noqa: E402
    from .call_logging import in_bulk_op, start_call_log  # noqa: E402

    from .async_api_ops.clean_meaning import clean_meaning_in_note  # noqa: E402
    from .async_api_ops.translate_field import translate_sentence_in_note  # noqa: E402
    from .async_api_ops.make_kanji_story import make_story_for_note  # noqa: E402
    from .async_api_ops.extract_words import extract_words_op  # noqa: E402
    from .async_api_ops.make_all_meanings import (  # noqa: E402
        load_meanings_dict_from_file,
        write_meanings_dict_to_file,
    )
    from .sync_local_ops.make_fine_tuning_data import make_all_test_data  # noqa: E402
    # The ops the browser menu runs, by way of the registry, which imports every op module
    from .ai_helper_menu import add_ai_helper_actions  # noqa: E402

    MISSING_PACKAGE: Optional[str] = None
except ImportError as error:
    # `name` is the module that could not be found, or the one a name could not be taken out
    # of; it is None only for an ImportError raised by hand, which none of the above does.
    MISSING_PACKAGE = error.name or "a package it vendors"

# VENDOR_HEALTH above compares what the vendored tree was built *for*. An import that has just
# failed is that tree answering for what is actually *in* it, so it is the more concrete of the
# two and names the package the rebuild has to fetch.
if MISSING_PACKAGE:
    VENDOR_HEALTH = f"{MISSING_PACKAGE} is missing from its vendored packages"


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

    # Captured once, when the menu opens: every action runs over the selection it was opened on
    selected_nids = browser.selectedNotes()

    ai_menu = menu.addMenu("AI helper")
    if ai_menu is None:
        logger.error("Error: AI helper menu could not be created.")
        return
    add_ai_helper_actions(ai_menu, selected_nids, parent=browser)


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
        # The generated meanings are what clean_meaning_in_note maps a note's meaning
        # against, and it revises them in place when none of them fit, so the one added
        # note reads the file and writes it back - the bulk ops do the same around a run.
        all_generated_meanings_dict = load_meanings_dict_from_file()
        try:
            clean_meaning_in_note(
                config, note, {}, notes_to_update_dict, all_generated_meanings_dict
            )
            write_meanings_dict_to_file(all_generated_meanings_dict)
            # The lexicon is read here rather than cached: the user rebuilds it from the
            # collection now and then, and one added note is one small json read.
            extract_words_op()(config, note, {}, notes_to_update_dict)
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


def add_tools_menu_actions():
    action = QAction("AI ops: generate test data", mw)
    qconnect(action.triggered, lambda: make_all_test_data(parent=mw))
    mw.form.menuTools.addAction(action)


# Every hook below calls something the guarded imports bind, so with a package missing each
# would raise NameError the first time Anki fired it - on adding a note, on opening a browser
# context menu - which reads as a broken Anki rather than an addon waiting on a rebuild. Leaving
# them unregistered is what "loads degraded" means here: the menus and the note hooks are simply
# absent until the rebuild below has run and Anki has been restarted.
if MISSING_PACKAGE is None:
    # Register to card adding hook
    hooks.note_will_be_added.append(lambda _col, note, _deck_id: run_op_on_add_note(note))

    # hooks.note_will_be_added.append(lambda _col, note, _deck_id: translate_sentence_in_note(
    # note, config=mw.addonManager.getConfig(__name__)))

    # Register to context menu initialization hook
    gui_hooks.browser_will_show_context_menu.append(on_browser_will_show_context_menu)

    # Register to field unfocus hook
    gui_hooks.editor_did_unfocus_field.append(run_op_on_field_unfocus)

    gui_hooks.main_window_did_init.append(add_tools_menu_actions)
else:
    logging.getLogger(__name__).warning(
        "loaded without its operations: %s could not be imported", MISSING_PACKAGE
    )

# Offer to rebuild the vendored packages when they do not fit this machine, and put the same
# rebuild in the Tools menu for anyone who wants rapidfuzz's compiled half - which the shipped
# lib/ leaves out, because five platforms of it is ~30 MB.
#
# MISSING_PACKAGE goes too, and is the difference between an optimisation and a repair. Anki
# cannot know that psutil only costs this addon a static concurrency limit while sudachipy
# costs it the whole word array, so the addon is what tells the dialog which it is looking at.
install_rebuild_ui(ADDON_DIR, ADDON_NAME, VENDOR_HEALTH, MISSING_PACKAGE)
