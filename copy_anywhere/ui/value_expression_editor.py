"""The one editor every format-2 stage that computes something is built from.

Format 1 repeated this widget group four times -- once in the field editor, once in the
file editor, once for variables, once for card actions -- because each one lived in its own
tab with its own labels. Format 2 has one `ValueExpression` shape (§4.1), so it has one
editor: a mode toggle, an interpolated text edit, a code edit, and the process chain that
runs after either.
"""

from typing import Callable, Optional, Sequence

from aqt.qt import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)

from ..configuration import ALL_FIELD_TO_FIELD_PROCESS_NAMES
from ..logic.definition_schema import (
    MODE_CODE,
    MODE_TEXT,
    ValueExpression,
    value_expression,
)
from ..logic.rename_scan import READ_AS_CODE, READ_AS_QUERY, READ_AS_TEXT
from ..shared.ui.code_edit_layout import CodeEditLayout
from ..shared.ui.interpolated_text_edit import InterpolatedTextEditLayout
from ..shared.ui.toggle_switch import ToggleSwitch
from .code_notices import FIELD_CODE_NOTICE
from .edit_extra_processing_dialog import EditExtraProcessingWidget
from .rename_indicator import LiveLocation, RenameIndicator
from .stage_document import StageDocument
from .stage_edit_state import StageEditState
from .stage_editor_context import StageEditorContext


class ValueExpressionEditor(QWidget):
    """Edits one `ValueExpression` in place.

    The expression dict handed in is the one the stage holds, and the process-chain editor
    writes straight into its `process_chain`. Text and mode are written back by `apply()`,
    which the dialog calls before it re-analyses or saves, so a half-typed expression never
    reaches the analyser mid-keystroke.
    """

    changed = pyqtSignal()

    def __init__(
        self,
        parent: QWidget,
        expression: Optional[ValueExpression],
        context: StageEditorContext,
        state: StageEditState,
        label: str,
        description: Optional[str] = None,
        notice: str = FIELD_CODE_NOTICE,
        is_required: bool = True,
        process_names: Optional[Sequence[str]] = None,
        allow_code: bool = True,
        allow_process_chain: bool = True,
        height: Optional[int] = None,
        placeholder_text: Optional[str] = None,
        rename_document: Optional[StageDocument] = None,
        rename_key: Optional[Callable[[str], str]] = None,
        text_is_search: bool = False,
    ) -> None:
        """`rename_key` turns `text` or `code` into the location key of that side of this
        expression (`rename_locations`, from the guid of the stage or field write that
        holds it), and with `rename_document` puts a rename indicator over the editor;
        only the owner knows whose expression this is. `text_is_search` says the text is
        an Anki search, which is read for search terms as well as references.
        """
        super().__init__(parent)
        self.expression: ValueExpression = expression if expression is not None else value_expression()
        self.expression.setdefault("mode", MODE_TEXT)
        self.expression.setdefault("text", "")
        self.expression.setdefault("code", "")
        self.expression.setdefault("process_chain", [])
        self.state = state
        self.context = context

        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(0, 0, 0, 0)

        # The mode toggle and the rename indicator share a line above both modes' boxes,
        # so the indicator stays in one place whichever box is showing.
        self.header = QHBoxLayout()
        self.header.setContentsMargins(0, 0, 0, 0)
        self.vbox.addLayout(self.header)
        self.use_code_toggle: Optional[ToggleSwitch] = None
        if allow_code:
            self.use_code_toggle = ToggleSwitch("Execute content as Python code", self)
            self.header.addWidget(self.use_code_toggle)

        self.text_container = QWidget(self)
        self.text_layout = InterpolatedTextEditLayout(
            parent=self.text_container,
            label=label,
            options_dict=context.options_dict,
            validate_dict=context.validate_dict,
            description=description,
            is_required=is_required,
            height=height,
            placeholder_text=placeholder_text,
        )
        self.vbox.addWidget(self.text_container)
        self.text_layout.set_text(self.expression.get("text", "") or "")

        self.code_layout: Optional[CodeEditLayout] = None
        if allow_code:
            self.code_layout = CodeEditLayout(
                parent=self,
                options_dict=context.options_dict,
                validate_dict=context.validate_dict,
                label=label,
                description=description,
                notice=notice,
                is_required=False,
            )
            self.code_layout.set_text(self.expression.get("code", "") or "")
            self.vbox.addWidget(self.code_layout)

        self.process_widget: Optional[EditExtraProcessingWidget] = None
        if allow_process_chain:
            self.process_widget = EditExtraProcessingWidget(
                self,
                None,
                self.expression,  # type: ignore[arg-type]
                list(process_names or ALL_FIELD_TO_FIELD_PROCESS_NAMES),
                state=state,
            )
            self.vbox.addWidget(self.process_widget)

        #: Whether the code half applies at all. `allow_code` settles it at build time;
        #: `set_code_allowed` moves it afterwards, for an owner whose own controls decide.
        self.code_is_allowed = allow_code
        self._apply_mode(self.expression.get("mode") == MODE_CODE)
        if self.use_code_toggle is not None:
            self.use_code_toggle.setChecked(self.expression.get("mode") == MODE_CODE)
            self.use_code_toggle.toggled.connect(self._on_mode_toggled)
        self.text_layout.text_edit.textChanged.connect(self.changed)
        if self.code_layout is not None:
            self.code_layout.text_edit.textChanged.connect(self.changed)

        self.text_is_search = text_is_search
        self.rename_indicator: Optional[RenameIndicator] = None
        if rename_document is not None and rename_key is not None:
            key = rename_key
            self.rename_indicator = RenameIndicator(
                self, rename_document, lambda: self._rename_locations(key)
            )
            self.header.addWidget(self.rename_indicator)
            self.changed.connect(self.rename_indicator.refresh)
        self.header.addStretch(1)

    # -- state ---------------------------------------------------------------------------

    def _rename_locations(self, key: Callable[[str], str]) -> list[LiveLocation]:
        """Both sides, with only the one `apply` would store as the mode in effect.

        The other side is kept for switching back but never runs, and the pass reads and a
        save keeps only the side that does (`rename_reconcile._expression_location`).
        """
        code = self.is_code_mode()
        text_read_as = READ_AS_QUERY if self.text_is_search else READ_AS_TEXT
        code_text = self.code_layout.get_text() if self.code_layout is not None else None
        return [
            LiveLocation(key("text"), text_read_as, None if code else self.text_layout.get_text()),
            LiveLocation(key("code"), READ_AS_CODE, code_text if code else None),
        ]

    def refresh_rename_indicator(self) -> None:
        if self.rename_indicator is not None:
            self.rename_indicator.refresh()

    def set_text_is_search(self, is_search: bool) -> None:
        """Read the text as an Anki search from now on, or stop: a condition can switch."""
        self.text_is_search = is_search
        self.refresh_rename_indicator()

    def _apply_mode(self, use_code: bool) -> None:
        self.text_container.setVisible(not use_code)
        if self.code_layout is not None:
            self.code_layout.setVisible(use_code)

    def _on_mode_toggled(self, checked: bool) -> None:
        self._apply_mode(checked)
        if checked and self.code_layout is not None and not self.code_layout.get_text().strip():
            # Seed the code with the text the user already wrote, quoted, so switching mode
            # is a starting point rather than a blank page.
            self.code_layout.set_text(
                f"value = {self.text_layout.get_text()!r}\nreturn value"
            )
        self.changed.emit()

    def set_code_allowed(self, allowed: bool) -> None:
        """Show or hide the code half after the fact.

        `allow_code` is settled when the widget is built and a stage whose kind the user can
        change while the dialog is open needs to move it: the condition stage switches
        between an Anki search, which has no code form at all, and an expression, which
        does. The toggle keeps the mode the user chose while it is hidden, so allowing code
        again brings their code back; it is `is_code_mode` that answers text while code is
        not allowed, so a save made in that state stores the text form, because code
        sitting behind a hidden toggle is code that would never run.
        """
        self.code_is_allowed = allowed
        if self.use_code_toggle is None:
            return
        self.use_code_toggle.setVisible(allowed)
        self._apply_mode(allowed and self.use_code_toggle.isChecked())
        self.refresh_rename_indicator()

    def is_code_mode(self) -> bool:
        return bool(
            self.code_is_allowed
            and self.use_code_toggle is not None
            and self.use_code_toggle.isChecked()
        )

    def apply(self) -> ValueExpression:
        """Write the widgets back into the expression dict and return it."""
        self.expression["mode"] = MODE_CODE if self.is_code_mode() else MODE_TEXT
        self.expression["text"] = self.text_layout.get_text()
        if self.code_layout is not None:
            self.expression["code"] = self.code_layout.get_text()
        return self.expression

    def set_context(self, context: StageEditorContext) -> None:
        """Point every menu in this editor at a new scope."""
        self.context = context
        self.state.set_context(context)
        self.text_layout.update_options(context.options_dict, context.validate_dict)
        if self.code_layout is not None:
            self.code_layout.update_options(context.options_dict, context.validate_dict)

    def set_label(self, label: str) -> None:
        """Say what belongs in the box, on both modes' captions.

        The condition stage relabels this when its kind changes -- an Anki search wants one
        thing, an expression another -- so it has to reach the caption the user reads. It
        used to set a tooltip, which said the right thing to anyone who hovered and left the
        caption saying whatever it was built with.
        """
        self.text_layout.set_label(label)
        if self.code_layout is not None:
            self.code_layout.set_label(label)
