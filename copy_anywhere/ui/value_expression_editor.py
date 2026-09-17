"""The one editor every format-2 stage that computes something is built from.

Format 1 repeated this widget group four times -- once in the field editor, once in the
file editor, once for variables, once for card actions -- because each one lived in its own
tab with its own labels. Format 2 has one `ValueExpression` shape (§4.1), so it has one
editor: a mode toggle, an interpolated text edit, a code edit, and the process chain that
runs after either.
"""

from typing import Optional, Sequence

from aqt.qt import (
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)

from ..configuration import ALL_FIELD_TO_FIELD_PROCESS_NAMES
from ..logic.definition_schema import MODE_CODE, MODE_TEXT, ValueExpression, value_expression
from ..shared.ui.code_edit_layout import CodeEditLayout
from ..shared.ui.interpolated_text_edit import InterpolatedTextEditLayout
from ..shared.ui.toggle_switch import ToggleSwitch
from .code_notices import FIELD_CODE_NOTICE
from .edit_extra_processing_dialog import EditExtraProcessingWidget
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
    ) -> None:
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

        self.use_code_toggle: Optional[ToggleSwitch] = None
        if allow_code:
            self.use_code_toggle = ToggleSwitch("Execute content as Python code", self)
            self.vbox.addWidget(self.use_code_toggle)

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
                state=state,  # type: ignore[arg-type]
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

    # -- state ---------------------------------------------------------------------------

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
        does. Taking code away also turns the mode back to text, because code sitting behind
        a hidden toggle is code that would never run.
        """
        self.code_is_allowed = allowed
        if self.use_code_toggle is None:
            return
        if not allowed and self.use_code_toggle.isChecked():
            self.use_code_toggle.setChecked(False)
        self.use_code_toggle.setVisible(allowed)
        if not allowed and self.code_layout is not None:
            self.code_layout.setVisible(False)

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
