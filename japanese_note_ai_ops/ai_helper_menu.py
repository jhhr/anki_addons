"""The browser context menu's "AI helper" entries: the multi-op dialog, the op registry and two
menu-only actions.

Kept out of `__init__.py` so that the menu's labels, order and wiring can be tested: the
add-on's test suite never imports `__init__.py`, which needs a running Anki.

Imports the op modules through `op_registry`, so it too may be imported only inside
`__init__.py`'s guarded `try/except ImportError` block.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from anki.notes import NoteId
from aqt import mw
from aqt.qt import QAction, QMenu, qconnect

from .multi_op_dialog import show_multi_op_dialog
from .op_registry import GROUP_ASYNC, GROUP_SYNC, OPS, OpSpec
from .sync_local_ops.build_name_lexicon import build_name_lexicon_from_selected
from .sync_local_ops.make_fine_tuning_data import make_kanjify_sentence_data


@dataclass(frozen=True)
class MenuOnlyAction:
    """A menu entry that is not an op: it writes no notes, so it is no step of a chain."""

    label: str
    run: Callable[[Sequence[NoteId], Any], None]
    # The op it follows in the menu
    after_key: str


MENU_ONLY_ACTIONS: tuple[MenuOnlyAction, ...] = (
    MenuOnlyAction(
        "Build name lexicon from selected notes",
        lambda nids, parent: build_name_lexicon_from_selected(nids, parent=parent),
        after_key="tag_notes_matched_status",
    ),
    MenuOnlyAction(
        "Export kanjify test data",
        lambda nids, parent: make_kanjify_sentence_data(nids, parent=parent),
        after_key="deduplicate_existing_meaning_notes",
    ),
)

# First in the submenu, above a separator of its own: it is no op, it opens the dialog
MULTI_OP_LABEL = "Run several ops..."

# None is the separator between the async and the sync ops
MenuEntry = Optional[Union[OpSpec, MenuOnlyAction]]


def menu_entries() -> list[MenuEntry]:
    entries: list[MenuEntry] = []
    for group in (GROUP_ASYNC, GROUP_SYNC):
        if entries:
            entries.append(None)
        for spec in OPS:
            if spec.group != group:
                continue
            entries.append(spec)
            entries.extend(a for a in MENU_ONLY_ACTIONS if a.after_key == spec.key)
    return entries


def _on_triggered(entry: Union[OpSpec, MenuOnlyAction], nids: Sequence[NoteId], parent: Any):
    # A closure per entry, not a lambda in the loop below: that would see the loop's last
    # entry by the time any action fires. Takes no arguments on purpose, like the lambdas it
    # replaced: PyQt passes `triggered`'s `checked` flag to a slot that can take it, and a
    # functools.partial can, which would hand the flag to the op as its chain.
    if isinstance(entry, OpSpec):
        return lambda: entry.start(nids, parent, None)
    return lambda: entry.run(nids, parent)


def add_ai_helper_actions(ai_menu: QMenu, nids: Sequence[NoteId], parent: Any) -> None:
    """Fill the "AI helper" submenu; every action runs over `nids`, the selection as it was
    when the menu opened. `parent` is the browser, which the dialog opens over."""
    open_dialog = QAction(MULTI_OP_LABEL, mw)
    qconnect(open_dialog.triggered, lambda: show_multi_op_dialog(parent))
    ai_menu.addAction(open_dialog)
    ai_menu.addSeparator()
    for entry in menu_entries():
        if entry is None:
            ai_menu.addSeparator()
            continue
        action = QAction(entry.label, mw)
        qconnect(action.triggered, _on_triggered(entry, nids, parent))
        ai_menu.addAction(action)
