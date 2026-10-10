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

from typing import Callable, Optional

from anki.models import NotetypeDict
from aqt.qt import sip

from ..configuration import (
    COPY_MODE_ACROSS_NOTES,
    COPY_MODE_WITHIN_NOTE,
    DIRECTION_SOURCE_TO_DESTINATIONS,
)
from .stage_editor_context import StageEditorContext

Callback = Callable[[], None]


class StageEditState:
    """What the reused editors think is an `EditState`.

    `copy_mode` and `copy_direction` are the only knobs with an effect, and only on two
    things: which card types a card-action editor offers, and the wording of the tag
    labels. Both menu dictionaries are the stage's own, so the process-chain dialogs see
    the same scope as the stage they belong to whichever branch they take.

    The reused editors register callbacks to hear about a changed note type, copy direction
    or variable list. Two things change while a stage's editor is open, and both are fired
    from here: the note the stage edits, chosen in the stage's own row, and the trigger's
    note types, chosen at the top of the same dialog. The variable and copy-on-sync
    registries are never fired -- a change that moves a stage's scope rebuilds the tree --
    but the widgets register on them regardless, so they have to exist.
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
        self.selected_model_callbacks: list[Callback] = []
        self.copy_direction_callbacks: list[Callback] = []
        self.variable_names_callbacks: list[Callback] = []
        self.copy_on_sync_callbacks: list[Callback] = []

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

    # -- what changes while the editor is open -------------------------------------------

    def set_selected_models(self, models: list[NotetypeDict]) -> None:
        """Follow the trigger's note types, and tell everything that lists by note type.

        Only a real change is announced: the context is refreshed after every edit anywhere
        in the dialog, and relisting the card types each time would clear a selector the
        user may be in the middle of using.
        """
        if [model.get("id") for model in models] == [
            model.get("id") for model in self.selected_models
        ]:
            return
        self.selected_models = list(models)
        self._fire(self.selected_model_callbacks)

    def set_target_is_trigger(self, target_is_trigger: bool) -> None:
        """Change which note the stage edits, and tell everything keyed on it.

        The target is a combo at the top of the stage's own row, so this is an ordinary edit
        rather than something fixed when the row was built: a stage retargeted from the
        trigger to a queried note has to stop offering the trigger note type's card types
        and start offering all of them, or an action added from the stale list names a card
        type the queried note will not have and matches nothing at run time. The tag
        captions and the paragraph over the card actions say which note they reach, and
        they read the same mode, so the direction registry is fired along with the note
        type one.
        """
        copy_mode = COPY_MODE_WITHIN_NOTE if target_is_trigger else COPY_MODE_ACROSS_NOTES
        if copy_mode == self.copy_mode:
            return
        self.copy_mode = copy_mode
        self._fire(self.selected_model_callbacks)
        self._fire(self.copy_direction_callbacks)

    @staticmethod
    def _fire(callbacks: list[Callback]) -> None:
        """Call every callback, dropping those whose widget Qt has already deleted.

        The registries outlive what registers on them: a regex dialog registers when its
        Edit button is first clicked and is `deleteLater`'d when its process or its whole
        field row is removed, and nothing takes the callback back out. Calling it then
        raises "wrapped C/C++ object ... has been deleted". Only that case is dropped;
        format 1's `call_callbacks` swallowed every exception, which would also hide a real
        bug in a live callback.
        """
        for callback in list(callbacks):
            if _owner_is_deleted(callback):
                callbacks.remove(callback)
                continue
            callback()

    # -- callback registries -------------------------------------------------------------

    def add_selected_model_callback(self, callback: Callback) -> None:
        self.selected_model_callbacks.append(callback)

    def add_copy_direction_callback(self, callback: Callback) -> None:
        self.copy_direction_callbacks.append(callback)

    def add_variable_names_callback(self, callback: Callback) -> None:
        self.variable_names_callbacks.append(callback)

    def add_copy_on_sync_callback(self, callback: Callback) -> None:
        self.copy_on_sync_callbacks.append(callback)


def _owner_is_deleted(callback: Callback) -> bool:
    """Whether `callback` is a method of a Qt object whose C++ side is gone."""
    owner = getattr(callback, "__self__", None)
    return isinstance(owner, sip.simplewrapper) and sip.isdeleted(owner)
