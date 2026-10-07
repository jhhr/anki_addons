import os
import logging
from typing import Optional

from anki import hooks
from anki.notes import Note, NoteId
from aqt import gui_hooks
from aqt import mw
from aqt.browser import Browser
from aqt.operations.scheduling import suspend_cards
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
    from .note_roles import LayoutError  # noqa: E402
    from .note_hooks import (  # noqa: E402
        CLEAN_MEANING,
        EXTRACT_WORDS,
        ops_on_added_note,
        suspends_added_cards,
        translates_on_unfocus,
    )
    from .configuration import ADDON_USER_FILES_DIR, capture_versions  # noqa: E402
    from .call_logging import current_log_path, in_bulk_op, start_call_log  # noqa: E402
    from .async_api_ops import capture  # noqa: E402

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
    from .multi_op_dialog import show_multi_op_dialog  # noqa: E402

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

    # A handler from the start, which does nothing. Until an op or a hook opens a log file the
    # addon logger has none, and a record that finds no handler goes to logging's last resort,
    # stderr, which Anki turns into an error dialog: an error logged before the first log file
    # was opened (at profile open, say) would reach the user as a crash report. Never flagged
    # as the addon's, so opening and closing log files leaves it in place. Here rather than in
    # call_logging, which imports vendored packages: a broken install must not write to stderr
    # either.
    if not any(isinstance(h, logging.NullHandler) for h in addon_logger.handlers):
        addon_logger.addHandler(logging.NullHandler())


setup_addon_logging()


# Function to be executed when the browser menus are initialized
def on_browser_will_show_context_menu(browser: Browser, menu: QMenu):
    logger = logging.getLogger(__name__)
    # Only for what building the menu logs, and it closes the last op's file. An op chosen from
    # the menu opens a file named after itself when it starts (ai_helper_menu)
    start_call_log("browser_menu")

    # Captured once, when the menu opens: every action runs over the selection it was opened on
    selected_nids = browser.selectedNotes()

    ai_menu = menu.addMenu("AI helper")
    if ai_menu is None:
        logger.error("Error: AI helper menu could not be created.")
        return
    add_ai_helper_actions(ai_menu, selected_nids, parent=browser)


def add_browser_edit_menu_action(browser: Browser):
    # The Edit menu rather than the context menu is the way in that avoids the lag: a right
    # click on thousands of selected rows is slow, and the dialog needs only one selected row
    # and the search
    config = mw.addonManager.getConfig(__name__) or {}
    menu = browser.form.menuEdit
    menu.addSeparator()
    action = QAction("Japanese AI ops...", browser)
    shortcut = config.get("multi_op_dialog_shortcut", "")
    if shortcut:
        action.setShortcut(shortcut)
    qconnect(action.triggered, lambda: show_multi_op_dialog(browser))
    menu.addAction(action)


def run_op_on_field_unfocus(changed: bool, note: Note, field_idx: int):
    logger = logging.getLogger(__name__)
    # A hook the user drives one field at a time, so the call really is the unit of work and a
    # log file per call is the right granularity - unlike note_will_be_added, which a bulk run
    # fires a thousand times in a row. The file is opened once the op is known, named by the
    # op's key as the menu's are, and not at all for the many unfocuses that run nothing.

    note_type = note.note_type()
    if not note_type:
        return
    note_type_name = note_type["name"]
    config = mw.addonManager.getConfig(__name__)
    if not config:
        start_call_log("field_unfocus")
        logger.error("Error: Missing addon configuration")
        return

    field_name = note_type["flds"][field_idx]["name"]
    cur_field_value = note[field_name]

    # The note in the editor may not be added yet (id 0): its calls then have no note, rather
    # than all sharing a note 0
    if note_type_name == "Kanji draw":
        story_field = get_field_config(config, "story_field", note_type)
        if field_name == story_field and cur_field_value == "":
            start_call_log("kanji_story")
            with capture.note_scope(note.id or None):
                return make_story_for_note(config, note, {}, {})

    if translates_on_unfocus(config, note_type_name):
        translated_sentence_field = get_field_config(config, "translated_sentence_field", note_type)
        if field_name == translated_sentence_field and cur_field_value == "":
            start_call_log("translate_sentence")
            with capture.note_scope(note.id or None):
                # The note the editor shows, which the editor saves. A sentence note's vocab
                # notes keep their copy of its old translation until "Refresh example sentences"
                return translate_sentence_in_note(config, note, {}, {}, copy_to_vocab_notes=False)


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

    # A note an op adds in its cleanup is added on the run's thread, inside its bulk op.
    # Nothing runs on it (ops_on_added_note), and the sentence migration adds thousands: this
    # answers before the note type and the config are read.
    op_adding = in_bulk_op()
    if op_adding:
        return

    logger = logging.getLogger(__name__)
    # A note added by hand, which is the case a log file per call was made for. Inside a bulk
    # op the run owns the handler and the phase it belongs to has already installed one;
    # replacing it per note is what produced 1,453 log files for a single run.
    start_call_log("add_note")

    note_type = note.note_type()
    if not note_type:
        return
    note_type_name = note_type["name"]
    config = mw.addonManager.getConfig(__name__)
    if not config:
        logger.error("Error: Missing addon configuration")
        return
    try:
        ops = ops_on_added_note(config, note_type_name, note.tags, op_adding=op_adding)
    except LayoutError as e:
        logger.error(f"Nothing run on the added note: {e}")
        return

    if ops:
        notes_to_update_dict: dict[NoteId, Note] = {}
        try:
            # Not added yet, so id 0: its calls are recorded with no note, not against note 0
            with capture.note_scope(note.id or None):
                if CLEAN_MEANING in ops:
                    # The generated meanings are what clean_meaning_in_note maps a note's
                    # meaning against, and it adds one in place when none of them fit, so the
                    # one added note reads the file and writes it back - the bulk ops do the
                    # same around a run.
                    all_generated_meanings_dict = load_meanings_dict_from_file()
                    clean_meaning_in_note(
                        config, note, {}, notes_to_update_dict, all_generated_meanings_dict
                    )
                    write_meanings_dict_to_file(all_generated_meanings_dict)
                if EXTRACT_WORDS in ops:
                    # The lexicon is read here rather than cached: the user rebuilds it from
                    # the collection now and then, and one added note is one small json read.
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


def suspend_added_sentence_note(note: Note):
    # After the Add dialog has added the note: note_will_be_added comes before Anki makes its
    # cards, and a sentence note gets at least one however its template renders. The Add
    # dialog only, so always a person's note; an op suspends the cards of the notes it adds.
    note_type = note.note_type()
    config = mw.addonManager.getConfig(__name__)
    if not note_type or not config or not suspends_added_cards(config, note_type["name"]):
        return
    card_ids = note.card_ids()
    if card_ids:
        suspend_cards(parent=mw, card_ids=card_ids).run_in_background()


def add_tools_menu_actions():
    action = QAction("AI ops: generate test data", mw)
    qconnect(action.triggered, lambda: make_all_test_data(parent=mw))
    mw.form.menuTools.addAction(action)


def install_capture_store():
    # Per profile, and the config is read only here: a change takes effect at the next profile
    # open. The store is diagnostics, so nothing here may keep a profile from opening.
    try:
        config = mw.addonManager.getConfig(__name__) or {}
        if not config.get("capture_calls", True):
            return
        capture.install(
            os.path.join(ADDON_USER_FILES_DIR, "capture.sqlite3"),
            keep_days=config.get("capture_keep_days", 90),
            versions=capture_versions(),
            log_path=current_log_path,
            profile=getattr(mw.pm, "name", None),
        )
    except Exception:
        logging.getLogger(__name__).warning("The capture store was not installed", exc_info=True)


def shutdown_capture_store():
    # Waits a moment for the writer to commit what is queued; never raises
    capture.shutdown()


# Every hook below calls something the guarded imports bind, so with a package missing each
# would raise NameError the first time Anki fired it - on adding a note, on opening a browser
# context menu - which reads as a broken Anki rather than an addon waiting on a rebuild. Leaving
# them unregistered is what "loads degraded" means here: the menus and the note hooks are simply
# absent until the rebuild below has run and Anki has been restarted.
if MISSING_PACKAGE is None:
    # Register to card adding hook
    hooks.note_will_be_added.append(lambda _col, note, _deck_id: run_op_on_add_note(note))
    gui_hooks.add_cards_did_add_note.append(suspend_added_sentence_note)

    # hooks.note_will_be_added.append(lambda _col, note, _deck_id: translate_sentence_in_note(
    # note, config=mw.addonManager.getConfig(__name__)))

    # Register to context menu initialization hook
    gui_hooks.browser_will_show_context_menu.append(on_browser_will_show_context_menu)
    gui_hooks.browser_menus_did_init.append(add_browser_edit_menu_action)

    # Register to field unfocus hook
    gui_hooks.editor_did_unfocus_field.append(run_op_on_field_unfocus)

    gui_hooks.main_window_did_init.append(add_tools_menu_actions)

    # The AI calls' record, user_files/capture.sqlite3, open while a profile is
    gui_hooks.profile_did_open.append(install_capture_store)
    gui_hooks.profile_will_close.append(shutdown_capture_store)
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
