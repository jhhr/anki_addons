"""A visible border around one repeated block of the editor: a stage, a field write, a
process, a card action.

Those blocks each carry a Remove or Delete button, and without a line around them it was
not clear which block a button belonged to. Qt's StyledPanel frame, which the stage rows
used, draws almost nothing under Anki's dark theme. `palette(mid)` follows the theme, and is
the colour the stage summaries already use for their muted text.
"""

from __future__ import annotations

from aqt.qt import QFrame

OUTLINE_STYLE = "border: 1px solid palette(mid); border-radius: 4px;"


def outline_frame(frame: QFrame, name: str) -> None:
    """Draw the border around `frame`, and around nothing inside it.

    The rule is scoped to the frame's object name: a bare `QFrame { ... }` would cascade to
    every QLabel and nested QFrame inside, which are QFrames too, and box each of them.
    """
    frame.setObjectName(name)
    frame.setStyleSheet(f"QFrame#{name} {{ {OUTLINE_STYLE} }}")
