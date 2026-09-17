"""An `EditState`-shaped view of one stage's scope, so the format-1 widgets can be reused.

`EditExtraProcessingWidget`, `CardActionsEditor` and `TagEditor` each read a handful of
attributes off `EditState`: two menu dictionaries, the copy mode and direction, and a couple
of callback registries. None of that is worth rewriting for format 2 -- the process chain
editor in particular is nine hundred lines of dialogs that work perfectly well -- so this
supplies exactly those attributes from a `StageEditorContext` instead.

It is deliberately not a subclass. `EditState` builds its menus by walking every note type
in the collection, which is the work a per-stage context has already done, and it carries
two dozen fields about a definition-wide mode that a stage does not have.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from anki.models import NotetypeDict

from ..configuration import (
    COPY_MODE_ACROSS_NOTES,
    COPY_MODE_WITHIN_NOTE,
    DIRECTION_SOURCE_TO_DESTINATIONS,
)
from .stage_editor_context import StageEditorContext


@dataclass
class CallbackEntry:
    """A callback and whether the widget that registered it is on screen.

    The reused editors register these to hear about a changed note type or variable list.
    Format 2 fires one of them: `set_target_is_trigger`, because the note a stage edits is
    chosen in that stage's own row and the card types on offer follow it. The rest are
    registered and never fired -- a stage's scope is otherwise fixed while its editor is
    open, and a change that moves it rebuilds the tree -- but the widgets register them
    regardless, so the registries have to exist and hold something callable.
    """

    callback: Callable
    is_visible: bool = False

    def __call__(self, *args, **kwargs):
        return self.callback(*args, **kwargs)


class StageEditState:
    """What the reused editors think is an `EditState`.

    `copy_mode` and `copy_direction` are the only knobs with an effect, and only on two
    things: which card types a card-action editor offers, and the wording of the tag
    labels. Both menu dictionaries are the stage's own, so the process-chain dialogs see
    the same scope as the stage they belong to whichever branch they take.
    """

    def __init__(
        self,
        context: StageEditorContext,
        selected_models: Optional[list[NotetypeDict]] = None,
        target_is_trigger: bool = True,
    ) -> None:
        self.context = context
        self.selected_models: list[NotetypeDict] = selected_models or []
        # A stage editing the trigger offers the trigger's own card types; a stage editing
        # a queried note cannot know the note type, so it offers all of them -- which is
        # the branch format 1 called "source to destinations".
        self.copy_mode = COPY_MODE_WITHIN_NOTE if target_is_trigger else COPY_MODE_ACROSS_NOTES
        self.copy_direction = DIRECTION_SOURCE_TO_DESTINATIONS
        # Format 2 has no card selection count: a stage that wants several notes loops.
        self.card_select_count = 1
        # Scalar results are already in the menu under "Variables", so there is no second
        # dictionary to merge in.
        self.variables_dict: dict = {}
        self.variables_validate_dict: dict = {}
        self.intersecting_fields: set = set()
        self.selected_model_callbacks: list[CallbackEntry] = []
        self.copy_direction_callbacks: list[CallbackEntry] = []
        self.variable_names_callbacks: list[CallbackEntry] = []
        self.copy_on_sync_callbacks: list[CallbackEntry] = []

    # -- menus ---------------------------------------------------------------------------

    @property
    def pre_query_menu_options_dict(self) -> dict:
        return self.context.options_dict

    @property
    def post_query_menu_options_dict(self) -> dict:
        return self.context.options_dict

    @property
    def pre_query_text_edit_validate_dict(self) -> dict:
        return self.context.validate_dict

    @property
    def post_query_text_edit_validate_dict(self) -> dict:
        return self.context.validate_dict

    def set_context(self, context: StageEditorContext) -> None:
        self.context = context

    def set_target_is_trigger(self, target_is_trigger: bool) -> None:
        """Change which note the stage edits, and tell everything that lists by note type.

        The target is a combo at the top of the stage's own row, so this is an ordinary edit
        rather than something fixed when the row was built: a stage retargeted from the
        trigger to a queried note has to stop offering the trigger note type's card types
        and start offering all of them, or an action added from the stale list names a card
        type the queried note will not have and matches nothing at run time.
        """
        copy_mode = COPY_MODE_WITHIN_NOTE if target_is_trigger else COPY_MODE_ACROSS_NOTES
        if copy_mode == self.copy_mode:
            return
        self.copy_mode = copy_mode
        for entry in self.selected_model_callbacks:
            entry.callback()

    # -- callback registries -------------------------------------------------------------

    def add_selected_model_callback(
        self, callback: Callable, is_visible: bool = False
    ) -> CallbackEntry:
        entry = CallbackEntry(callback, is_visible)
        self.selected_model_callbacks.append(entry)
        return entry

    def add_copy_direction_callback(
        self, callback: Callable, is_visible: bool = False
    ) -> CallbackEntry:
        entry = CallbackEntry(callback, is_visible)
        self.copy_direction_callbacks.append(entry)
        return entry

    def add_variable_names_callback(
        self, callback: Callable, is_visible: bool = False
    ) -> CallbackEntry:
        entry = CallbackEntry(callback, is_visible)
        self.variable_names_callbacks.append(entry)
        return entry

    def add_copy_on_sync_callback(
        self, callback: Callable, is_visible: bool = False
    ) -> CallbackEntry:
        entry = CallbackEntry(callback, is_visible)
        self.copy_on_sync_callbacks.append(entry)
        return entry
