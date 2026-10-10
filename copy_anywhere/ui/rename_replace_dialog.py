"""What the Replace button beside a rename indicator would write, shown before it is written.

A rename in Anki leaves a warning on every text of a definition that still spells the old
name (`rename_indicator.py`). Where the scanners know how to spell the new name there
(`rename_scan`), the part offers to put it in, but only through this dialog: a search or a
piece of code rewritten out of sight is exactly the silent change the warnings exist to
prevent, and a rewrite of a search that quotes or escapes the name differently is easy to
misread. So the dialog shows the part's whole text with each old spelling struck through
and its replacement beside it, and has nothing to edit: Apply writes what it shows, Close
writes nothing.

What it shows is computed once, from the same hits and the same `rename_scan.apply` that
Apply writes, so two hits that overlap, where `apply` keeps the first, cannot make the
dialog promise a change that Apply then leaves out. Whatever is left (a deletion has no new
name, an f-string is never rewritten, an overlapped hit waits for a second Replace) is
listed under the diff to be fixed by hand.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from aqt.qt import (
    QDialog,
    QFontDatabase,
    QHBoxLayout,
    QLabel,
    QPalette,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..logic.rename_scan import (
    READ_AS_CODE,
    READ_AS_QUERY,
    READ_AS_TEXT,
    Hit,
    Rename,
    applied_hits,
    apply_with_spans,
    entry_hits,
    names_equal,
)
from .labels import wrapping

#: The locations whose value is a text with spans in it; every other kind is a slot that
#: holds whole names (one, or a list of them).
TEXT_READS = (READ_AS_TEXT, READ_AS_QUERY, READ_AS_CODE)

#: The parts of a diff, as `diff_pieces` labels them.
SAME, OLD, NEW = "same", "old", "new"


@dataclass
class Replacement:
    """What Replace would do to one location's value.

    `after` is what Apply writes. For a text, `written` pairs each hit replaced with where
    its replacement stands in `after`; for a slot, `swapped` lists each name and the one
    that takes its place. `left` says, a sentence each, what stays for the user to fix.

    `settled` lists the entries Apply answers that a later scan could not see answered:
    every spelling of the entry's old name was replaced, but its old name is also another
    entry's new one here -- a swap, `Word` → `Term` and `Term` → `Word`, or a chain -- so the
    replaced text still spells it. The part tells the document (`StageDocument.
    settle_rename_marks`), which hides them and lets a save drop them while the text is not
    put back as it was.
    """

    read_as: str
    before: Any
    after: Any
    written: list[tuple[Hit, tuple[int, int]]] = field(default_factory=list)
    swapped: list[tuple[str, str]] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    settled: list[dict] = field(default_factory=list)

    @property
    def changes(self) -> bool:
        return bool(self.written or self.swapped)


def _usable(entries: Sequence[dict]) -> list[tuple[dict, Rename]]:
    """The entries with a rename to act on, each with it."""
    found = []
    for entry in entries:
        rename = Rename.from_entry(entry)
        if rename is not None:
            found.append((entry, rename))
    return found


def _in_a_chain(rename: Rename, renames: Sequence[Rename]) -> bool:
    """Whether another rename here gives `rename`'s old name as its new one."""
    return any(
        other is not rename
        and other.kind == rename.kind
        and other.new is not None
        and names_equal(rename.kind, other.new, rename.old)
        for other in renames
    )


def _settled(
    usable: Sequence[tuple[dict, Rename]], hits_of: dict[int, list[Hit]], done: list[Hit]
) -> list[dict]:
    """The chained entries all of whose hits Apply writes (`Replacement.settled`)."""
    renames = [rename for _entry, rename in usable]
    return [
        entry
        for entry, rename in usable
        if hits_of.get(id(entry))
        and _in_a_chain(rename, renames)
        and all(hit in done for hit in hits_of[id(entry)])
    ]


def _where(text: str, hit: Hit) -> str:
    """The spelling a hit covers, with its line when the text has more than one."""
    spelled = f"“{text[hit.start : hit.end]}”"
    if "\n" not in text:
        return spelled
    return f"line {text.count(chr(10), 0, hit.start) + 1}: {spelled}"


def _why_left(rename: Rename, hit: Hit) -> str:
    if rename.new is None:
        return f"“{rename.old}” was deleted, so there is no new name to put here"
    if not hit.replaceable:
        return "this spelling cannot be rewritten automatically"
    return "it overlaps another replacement; press Replace again afterwards"


def _plan_text(read_as: str, text: str, usable: Sequence[tuple[dict, Rename]]) -> Replacement:
    found: list[tuple[Hit, Rename]] = []
    hits_of: dict[int, list[Hit]] = {}
    for entry, rename in usable:
        # The entry's own hits: a definition's `{{trigger.Word}}` is not a spelling of
        # another note type's `Word` its warning is about, and must not be rewritten to it.
        hits_of[id(entry)] = entry_hits(read_as, text, entry)
        for hit in hits_of[id(entry)]:
            # Two warnings about one object (a field renamed the same way in two note types)
            # find the same spelling twice; it is one replacement, not an overlap.
            if all(hit != seen for seen, _rename in found):
                found.append((hit, rename))
    hits = [hit for hit, _rename in found]
    after, spans = apply_with_spans(text, hits)
    written = list(zip(applied_hits(hits), spans))
    replaced = [hit for hit, _span in written]
    left = [
        f"{_where(text, hit)}: {_why_left(rename, hit)}"
        for hit, rename in found
        if hit not in replaced
    ]
    return Replacement(
        read_as,
        text,
        after,
        written=written,
        left=left,
        settled=_settled(usable, hits_of, replaced),
    )


def _plan_slot(read_as: str, value: Any, usable: Sequence[tuple[dict, Rename]]) -> Replacement:
    names = list(value) if isinstance(value, list) else [value]
    after: list[Any] = []
    swapped: list[tuple[str, str]] = []
    left: list[str] = []
    hits_of: dict[int, list[Hit]] = {}
    done: list[Hit] = []
    for name in names:
        new_name = name
        for entry, rename in usable:
            hits = entry_hits(read_as, name, entry)
            if not hits:
                continue
            hits_of.setdefault(id(entry), []).extend(hits)
            replacement = hits[0].replacement
            if replacement is None:
                left.append(f"“{name}”: {_why_left(rename, hits[0])}")
            else:
                new_name = replacement
                swapped.append((name, replacement))
                done.extend(hits)
            break
        # A list that already held the new name as well does not end up holding it twice.
        if new_name not in after:
            after.append(new_name)
    return Replacement(
        read_as,
        value,
        after if isinstance(value, list) else after[0],
        swapped=swapped,
        left=left,
        settled=_settled(usable, hits_of, done),
    )


def plan_replacement(read_as: str, value: Any, entries: Sequence[dict]) -> Replacement:
    """What Replace would write at a location reading `value` for these warnings.

    Every warning with a usable rename is applied in the one go, so a search naming both a
    renamed deck and a renamed note type is fixed by one Apply.
    """
    usable = _usable(entries)
    if read_as in TEXT_READS:
        text = value if isinstance(value, str) else ""
        return _plan_text(read_as, text, usable)
    if value is None or value == [] or value == "":
        return Replacement(read_as, value, value)
    return _plan_slot(read_as, value, usable)


def diff_pieces(replacement: Replacement) -> list[tuple[str, str]]:
    """A text replacement as runs of unchanged, removed and inserted text, in reading order.

    The unchanged runs and the inserted ones are cut from `after`, the text Apply writes,
    so the dialog with its struck-through runs taken out reads exactly as that text does.
    """
    before, after = replacement.before, replacement.after
    pieces: list[tuple[str, str]] = []
    position = 0
    for hit, (start, end) in replacement.written:
        if start > position:
            pieces.append((SAME, after[position:start]))
        pieces.append((OLD, before[hit.start : hit.end]))
        pieces.append((NEW, after[start:end]))
        position = end
    if position < len(after):
        pieces.append((SAME, after[position:]))
    return pieces


def diff_html(replacement: Replacement, palette: QPalette) -> str:
    """The diff as rich text, in colours taken from `palette` so it reads in either theme.

    The struck-through run is also dimmed and the inserted one is also bold, so neither
    depends on colour alone.
    """
    dim = palette.color(QPalette.ColorRole.PlaceholderText).name()
    marked = palette.color(QPalette.ColorRole.Highlight).name()
    on_marked = palette.color(QPalette.ColorRole.HighlightedText).name()
    styles = {
        SAME: "",
        OLD: f"text-decoration: line-through; color: {dim};",
        NEW: f"font-weight: bold; background-color: {marked}; color: {on_marked};",
    }
    body = "".join(
        f"<span style='{styles[kind]}'>{html.escape(text)}</span>"
        if styles[kind]
        else html.escape(text)
        for kind, text in diff_pieces(replacement)
    )
    return f"<div style='white-space: pre-wrap'>{body}</div>"


def swap_html(replacement: Replacement) -> str:
    """A slot's replacement: one `old → new` line per name it swaps."""
    return "<br>".join(
        f"“{html.escape(old)}” &rarr; <b>“{html.escape(new)}”</b>"
        for old, new in replacement.swapped
    )


class RenameReplaceDialog(QDialog):
    """Shows each replacement read-only; Apply hands every one to its part, Close does not.

    `replacements` pairs what to show with the part's own way of taking the new value, so
    a text box can put it through its undo stack and a picker can select the name.
    """

    def __init__(
        self,
        parent: Optional[QWidget],
        replacements: Sequence[tuple[Replacement, Callable[[Any], None]]],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Replace the old name")
        self.replacements = list(replacements)
        layout = QVBoxLayout(self)
        texts = [
            replacement
            for replacement, _write in self.replacements
            if replacement.changes and replacement.read_as in TEXT_READS
        ]
        intro = (
            "Apply writes the text below into the editor: struck-through text goes and"
            " highlighted text takes its place. Nothing changes until you press Apply,"
            " and Ctrl+Z in the text box undoes it afterwards."
            if texts
            else "Apply selects the new name in place of the old one. Nothing changes until"
            " you press Apply."
        )
        layout.addWidget(wrapping(QLabel(intro, self)))
        #: One read-only view per text replacement, one label per slot replacement.
        self.views: list[QWidget] = []
        left: list[str] = []
        for replacement, _write in self.replacements:
            left.extend(replacement.left)
            if not replacement.changes:
                continue
            if replacement.read_as in TEXT_READS:
                view = QTextBrowser(self)
                if replacement.read_as == READ_AS_CODE:
                    view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
                view.setHtml(diff_html(replacement, self.palette()))
                self.views.append(view)
                layout.addWidget(view)
            else:
                label = QLabel(swap_html(replacement), self)
                self.views.append(label)
                layout.addWidget(label)
        self.left_label: Optional[QLabel] = None
        if left:
            self.left_label = wrapping(
                QLabel(
                    "Left as it is, to fix by hand:<ul>"
                    + "".join(f"<li>{html.escape(line)}</li>" for line in left)
                    + "</ul>",
                    self,
                )
            )
            layout.addWidget(self.left_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.apply_button = QPushButton("Apply", self)
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.apply)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.resize(560, 320)

    def apply(self) -> None:
        for replacement, write in self.replacements:
            if replacement.changes:
                write(replacement.after)
        self.accept()


__all__ = [
    "Replacement",
    "RenameReplaceDialog",
    "diff_html",
    "diff_pieces",
    "plan_replacement",
    "swap_html",
]
