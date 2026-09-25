"""When the reconcile pass runs.

Twice, because between them they cover every way a rename can reach this profile without a
hook that names anything (`logic/rename_reconcile.py`): when the collection is opened, which
catches a rename made on another device and synced in before Anki started, and after any
operation that reported changing a note type or a deck, which catches the Fields, Card
Types, Note Types and deck dialogs, their undo and redo -- undo is a `CollectionOp` and
reports the same booleans -- and the everything-changed operation `mw.reset()` synthesises
after a sync.

Both run the pass synchronously on the main thread. It is two name listings plus one
`models.get` per referenced note type, which is not worth a background task, and a config
rewritten from a worker thread while the editor reads it is worth even less.
"""

import html
import logging
from typing import Optional

from aqt import mw
from aqt.gui_hooks import collection_did_load, operation_did_execute
from aqt.utils import showWarning

from ..configuration import Config
from ..logging_setup import operation_logging
from ..logic.rename_reconcile import (
    ReconcileResult,
    definitions_hold_references,
    log_result,
    reconcile,
)

logger = logging.getLogger(__name__)

#: What the last pass found, for the editor and the picker to mark a definition with. The
#: pass runs long before either is opened, and re-running it to ask would cost a save.
_last_result = ReconcileResult()

#: The pass writes the config, and a config write must never start another pass. Nothing in
#: Anki turns `writeConfig` into an operation today, so this is a guard rather than a fix.
_running = False


def last_reconcile_result() -> ReconcileResult:
    return _last_result


def run_reconcile() -> Optional[ReconcileResult]:
    """Reconcile the stored definitions against the collection, reporting into one log file.

    Returns what the pass found, or None when it did not run.

    Nothing here may raise: `operation_did_execute` removes a hook that does, so an
    exception would take the pass out for the rest of the session without saying so.
    """
    global _running, _last_result
    if _running or mw.col is None:
        return None
    _running = True
    try:
        # Reading the config is inside the try like everything else: a `meta.json` Anki
        # cannot parse, or a definition stored in a shape the scan chokes on, would
        # otherwise take the pass out for the rest of the session.
        config = Config()
        config.load()
        if not definitions_hold_references(config.copy_definitions):
            return None
        # The main window clears this in its own handler, but hook order is not something
        # to rely on: a note type read through a stale cache would compare equal to its
        # old name.
        mw.col.models._clear_cache()
        with operation_logging("rename_reconcile", config.log_level):
            _last_result = reconcile(config, mw.col)
            log_result(_last_result)
        return _last_result
    except Exception:
        logger.exception("Could not reconcile the stored definitions with the collection")
        return None
    finally:
        _running = False


def broken_definitions_warning(result: ReconcileResult) -> Optional[str]:
    """What to tell the user about definitions a field rename left marked, if any.

    One line per marked field, under the definition's name, so the user sees which of the
    three ways out -- rename the field in the other note types too, undo the rename, or
    rework the definition -- fits each one.
    """
    if not result.broken:
        return None
    lines = [
        html.escape(f"'{stale.definition_name}': {stale.message}") for stale in result.broken
    ]
    return (
        "These copy definitions trigger on several note types and spell a field that is"
        " not on all of them any more, so the field rename was not followed into them:"
        "<br><br>"
        + "<br>".join(lines)
        + "<br><br>Rename the field in the other note types too, undo the rename, or edit"
        " the definition. A definition is updated by itself once its note types agree"
        " again."
    )


def on_collection_did_load(col) -> None:
    """A collection was opened. A rename made on another device arrives with no hook at all
    -- only the collection, already holding the new names -- so this is where it is seen."""
    run_reconcile()


def on_operation_did_execute(changes, handler) -> None:
    """Anki says only *that* a note type or a deck changed, which is reason enough to look.

    After a note type change -- saving the Fields dialog is one -- a definition still
    marked as broken by a rename is said so in a dialog, since the log is not read at the
    default level. Not after a deck change: answering a card reports one, and a dialog on
    every answer would teach the user to dismiss it.
    """
    notetype_changed = bool(getattr(changes, "notetype", False))
    if not (notetype_changed or getattr(changes, "deck", False)):
        return
    result = run_reconcile()
    if not notetype_changed or result is None:
        return
    try:
        text = broken_definitions_warning(result)
        if text is not None:
            showWarning(text, parent=mw, title="CopyAnywhere", textFormat="rich")
    except Exception:
        logger.exception("Could not show the definitions a rename left broken")


def init_rename_hooks():
    collection_did_load.append(on_collection_did_load)
    operation_did_execute.append(on_operation_did_execute)
