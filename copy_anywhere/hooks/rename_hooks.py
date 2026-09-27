"""When the reconcile pass runs.

Twice, because between them they cover every way a rename can reach this profile without a
hook that names anything (`logic/rename_reconcile.py`): when the collection is opened, which
catches a rename made on another device and synced in before Anki started, and after any
operation that reported changing a note type or a deck, which catches the Fields, Card
Types, Note Types and deck dialogs, their undo and redo -- undo is a `CollectionOp` and
reports the same booleans -- and the everything-changed operation `mw.reset()` synthesises
after a sync.

A deck change alone runs it only when the decks' ids or names differ from what the last
completed pass saw. Answering a card reports a deck change (`col.sched.answer_card` says
`deck` changed, for the deck's review counts), so without that check every answer would
load the config from disk, clear the note type cache and scan every definition to find
nothing. The check costs one deck listing; a note type change always runs the pass.

Both run the pass synchronously on the main thread. It is two name listings plus one
`models.get` per referenced note type, which is not worth a background task, and a config
rewritten from a worker thread while the editor reads it is worth even less.
"""

from __future__ import annotations

import html
import logging
import weakref
from typing import Any, Optional

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

#: The pass writes the config, and a config write must never start another pass. Nothing in
#: Anki turns `writeConfig` into an operation today, so this is a guard rather than a fix.
_running = False

#: The collection the last completed pass ran on, and its decks then as `(id, name)` pairs.
#: A weak reference, so that it neither keeps a closed profile's collection alive nor lets
#: a new collection pass for the old one because it happens to reuse its `id()`.
_decks_seen: Optional[tuple[weakref.ref[Any], frozenset[tuple[int, str]]]] = None


def _deck_names(col: Any) -> frozenset[tuple[int, str]]:
    return frozenset((entry.id, entry.name) for entry in col.decks.all_names_and_ids())


def _remember_decks(col: Any) -> None:
    global _decks_seen
    _decks_seen = (weakref.ref(col), _deck_names(col))


def _forget_decks() -> None:
    global _decks_seen
    _decks_seen = None


def _decks_changed(col: Any) -> bool:
    """Whether `col`'s decks differ from what the last completed pass saw.

    One deck listing and nothing else -- not the config, not the note type cache -- since
    it is asked after every answer. A collection no pass has completed on counts as changed.
    """
    if _decks_seen is None:
        return True
    seen_col, seen_names = _decks_seen
    return seen_col() is not col or _deck_names(col) != seen_names


def run_reconcile() -> Optional[ReconcileResult]:
    """Reconcile the stored definitions against the collection, reporting into one log file.

    Returns what the pass found, or None when it did not run.

    The decks are remembered after every pass that completed, one that stopped because no
    definition holds a reference included: otherwise that is the pass the first answer
    after loading would repeat. A pass that failed remembers nothing, so the next deck
    change tries again.

    Nothing here may raise: `operation_did_execute` removes a hook that does, so an
    exception would take the pass out for the rest of the session without saying so.
    """
    global _running
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
            _remember_decks(mw.col)
            return None
        # The main window clears this in its own handler, but hook order is not something
        # to rely on: a note type read through a stale cache would compare equal to its
        # old name.
        mw.col.models._clear_cache()
        with operation_logging("rename_reconcile", config.log_level):
            result = reconcile(config, mw.col)
            log_result(result)
        _remember_decks(mw.col)
        return result
    except Exception:
        _forget_decks()
        logger.exception("Could not reconcile the stored definitions with the collection")
        return None
    finally:
        _running = False


def broken_definitions_warning(result: ReconcileResult) -> Optional[str]:
    """What to tell the user about the blocking marks this pass added, if any.

    Only the new ones (`ReconcileResult.newly_marked`), and only those that keep a
    definition from running, which is what this dialog says: a mark stays until the user
    replaces the name and saves or dismisses it, and listing it again after every unrelated
    note type edit would teach them to close this dialog unread. The picker and the editor go on showing every mark,
    and the log lists them all.

    One line per mark, under the definition's name: which field, card type, deck or note
    type was renamed or deleted. A definition spelling the name in several places has a
    mark at each, which the editor shows where it is; here they would be the same line
    again, so each line is said once. It says the definitions are not run meanwhile
    (`copy_fields.refused_for_rename`) and how that ends -- the user replaces the old name
    and saves, or dismisses the warning, in the definition editor -- since this dialog is
    where the user learns that.
    """
    blocking = [stale for stale in result.newly_marked if stale.blocks_run]
    if not blocking:
        return None
    lines = list(
        dict.fromkeys(
            html.escape(f"'{stale.definition_name}': {stale.message}") for stale in blocking
        )
    )
    return (
        "These copy definitions use a field, card type, deck or note type that was renamed or"
        " deleted, in a way that could not be followed into them, so they are marked and not"
        " run:"
        "<br><br>"
        + "<br>".join(lines)
        + "<br><br>In the definition editor, replace the old name and save, or dismiss the"
        " warning; a definition runs again once no blocking warning is left."
    )


def on_collection_did_load(col: Any) -> None:
    """A collection was opened. A rename made on another device arrives with no hook at all
    -- only the collection, already holding the new names -- so this is where it is seen.

    The decks an earlier collection had are forgotten first, so that nothing about it can
    stand in for this one's should this pass not complete.
    """
    _forget_decks()
    run_reconcile()


def on_operation_did_execute(changes: Any, handler: Any) -> None:
    """Anki says only *that* a note type or a deck changed, which is reason enough to look
    -- at a deck change, once the decks are seen to differ (see the module docstring).

    After either -- saving the Fields dialog, renaming a deck -- a mark the pass just added
    is said so in a dialog, since the log is not read at the default level. A deck rename
    marks a definition whose search or code spells the deck's old name. An answer, which
    reports a deck change too, never gets this far: its decks are the ones already seen.
    """
    notetype_changed = bool(getattr(changes, "notetype", False))
    if not notetype_changed:
        if not getattr(changes, "deck", False) or _running or mw.col is None:
            return
        try:
            if not _decks_changed(mw.col):
                return
        except Exception:
            logger.exception("Could not compare the decks with the last reconcile pass")
            return
    result = run_reconcile()
    if result is None:
        return
    try:
        text = broken_definitions_warning(result)
        if text is not None:
            showWarning(text, parent=mw, title="CopyAnywhere", textFormat="rich")
    except Exception:
        logger.exception("Could not show the definitions a rename left broken")


def init_rename_hooks() -> None:
    collection_did_load.append(on_collection_did_load)
    operation_did_execute.append(on_operation_did_execute)
