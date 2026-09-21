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

import logging

from aqt import mw
from aqt.gui_hooks import collection_did_load, operation_did_execute

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


def run_reconcile() -> None:
    """Reconcile the stored definitions against the collection, reporting into one log file.

    Nothing here may raise: `operation_did_execute` removes a hook that does, so an
    exception would take the pass out for the rest of the session without saying so.
    """
    global _running, _last_result
    if _running or mw.col is None:
        return
    config = Config()
    config.load()
    if not definitions_hold_references(config.copy_definitions):
        return
    # The main window clears this in its own handler, but hook order is not something to
    # rely on: a note type read through a stale cache would compare equal to its old name.
    mw.col.models._clear_cache()
    _running = True
    try:
        with operation_logging("rename_reconcile", config.log_level):
            _last_result = reconcile(config, mw.col)
            log_result(_last_result)
    except Exception:
        logger.exception("Could not reconcile the stored definitions with the collection")
    finally:
        _running = False


def on_collection_did_load(col) -> None:
    """A collection was opened. A rename made on another device arrives with no hook at all
    -- only the collection, already holding the new names -- so this is where it is seen."""
    run_reconcile()


def on_operation_did_execute(changes, handler) -> None:
    """Anki says only *that* a note type or a deck changed, which is reason enough to look."""
    if not (getattr(changes, "notetype", False) or getattr(changes, "deck", False)):
        return
    run_reconcile()


def init_rename_hooks():
    collection_did_load.append(on_collection_did_load)
    operation_did_execute.append(on_operation_did_execute)
