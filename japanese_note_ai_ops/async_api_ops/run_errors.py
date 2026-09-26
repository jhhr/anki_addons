"""The errors a run met, kept as data: for the progress dialog's pane and for the run's end.

Most errors do not fail a run. One note's request is refused, a provider's answer cannot be
read, one note's op raises; the run logs it and goes on with the others. Those used to reach
the log only. Now they are reported here as they happen, from whichever thread met them, and
`progress_errors` shows them in the pane beside the progress (it installs the delivery with
`deliver_with`; until it has, a report is only logged).

The pane closes with the dialog, and a chain's failed step closes it a moment after its error
arrives, so the errors are also kept for the run: `start_run` when a run from the menu or a
chain begins, `take_run` when its end message is shown, which lists them. Between a `take_run`
and the next `start_run` nothing is kept, so a late report from a thread a cancelled run
abandoned never shows up in the next run's end.

One failing cause can fail every note of a run alike (a wrong API key, a quota spent). An
error with the same text as an earlier one is counted with it, naming where it happened
again, instead of being listed thousands of times; past MAX_KINDS different ones the rest are
only counted.

Free of aqt and anki, so that `terminal_client` can report through it. Delivery and the store
are main thread only; the `report_*` functions may be called from any thread.
"""

from __future__ import annotations

import html
import logging
import re
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Different errors listed, in the pane and at the end; tens of distinct tracebacks are already
# more than anyone reads, and the log has every one
MAX_KINDS = 50
# Places named for an error that happened again; the count says how many there were
MAX_PLACES = 10
# A provider's error body can be a whole HTML page
MAX_DETAIL = 500

TITLE_JOIN = " · "


def excerpt(text: str, limit: int = MAX_DETAIL) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


class RunError:
    """One error, and how many more times the same text was reported, and where."""

    __slots__ = ("title", "text", "count", "places")

    def __init__(self, title: str, text: str) -> None:
        self.title = title
        self.text = text
        self.count = 1
        # Titles of the repeats, the first MAX_PLACES of them
        self.places: list[str] = []

    def again_line(self) -> str:
        """ "Also at 3 more: ..." for a repeated error, else ""."""
        more = self.count - 1
        if not more:
            return ""
        line = f"Also at {more} more: " + ", ".join(self.places)
        if more > len(self.places):
            line += ", …"
        return line


class ErrorList:
    """Errors grouped by their text, the first MAX_KINDS of them listed."""

    def __init__(self, max_kinds: int = MAX_KINDS) -> None:
        self.max_kinds = max_kinds
        self.entries: list[RunError] = []
        self._by_text: dict[str, RunError] = {}
        self.total = 0
        # Errors of the kinds past max_kinds
        self.not_listed = 0

    def add(self, title: str, text: str) -> Optional[RunError]:
        """Count an error; the new entry when it is listed as one, None when it was counted
        with an earlier one or not listed."""
        self.total += 1
        text = text.rstrip()
        same = self._by_text.get(text)
        if same is not None:
            same.count += 1
            if len(same.places) < MAX_PLACES:
                same.places.append(title)
            return None
        if len(self.entries) >= self.max_kinds:
            self.not_listed += 1
            return None
        entry = RunError(title, text)
        self.entries.append(entry)
        self._by_text[text] = entry
        return entry

    def header(self) -> str:
        return "1 error" if self.total == 1 else f"{self.total} errors"

    def not_listed_line(self) -> str:
        if not self.not_listed:
            return ""
        errors = "error" if self.not_listed == 1 else "errors"
        return f"{self.not_listed} more {errors} not listed here; the log has them."

    def as_text(self) -> str:
        """Every listed error in plain text, for a box the user can read and copy."""
        parts = [f"{self.header()} during the run."]
        for entry in self.entries:
            lines = [entry.title]
            again = entry.again_line()
            if again:
                lines.append(again)
            lines.append("")
            lines.append(entry.text)
            parts.append("\n".join(lines))
        if self.not_listed:
            parts.append(self.not_listed_line())
        return ("\n\n" + "-" * 40 + "\n\n").join(parts)

    def as_html(self) -> str:
        """The listed errors as the pane shows them."""
        listed = "".join(entry_html(e, i > 0) for i, e in enumerate(self.entries))
        if self.not_listed:
            listed += f"<hr><p><i>{html.escape(self.not_listed_line())}</i></p>"
        return listed


def entry_html(entry: RunError, separated: bool) -> str:
    # pre-wrap keeps a traceback's lines and indentation and still wraps the long ones
    again = entry.again_line()
    return (
        ("<hr>" if separated else "")
        + f"<p><b>{html.escape(entry.title)}</b></p>"
        + (f"<p><i>{html.escape(again)}</i></p>" if again else "")
        + f'<p style="white-space: pre-wrap;">{html.escape(entry.text)}</p>'
    )


# --- The run's errors ---------------------------------------------------------------------

_run: Optional[ErrorList] = None
# The chain step running now, prefixed to what the report_* functions name. A plain global,
# read from worker threads: steps run one after another, and a step's reports are delivered
# before its end is.
_step = ""


def start_run() -> None:
    """Keep the errors reported from now on, until `take_run`. Main thread."""
    global _run, _step
    _run = ErrorList()
    _step = ""


def set_step(title: str) -> None:
    """Name the chain step whose errors are reported from now on ("Step 2/4: Make meanings")."""
    global _step
    _step = title


def take_run() -> Optional[ErrorList]:
    """The errors kept since `start_run`, None if there were none; stops keeping them. Main
    thread."""
    global _run, _step
    errors, _run, _step = _run, None, ""
    return errors if errors is not None and errors.total else None


def record(title: str, text: str) -> None:
    """Keep an error the pane has shown, when a run is keeping them. Main thread."""
    if _run is not None:
        _run.add(title, text)


# --- Where an error happened --------------------------------------------------------------

# What a task is working on, set around the creation of its asyncio task: the task copies the
# context, and asyncio.to_thread hands it on to the worker thread running the op. Anything the
# op reports then names its note without being given it.
_subject: ContextVar[Any] = ContextVar("run_error_subject", default=None)

_TAG = re.compile(r"<[^>]*>")
SUBJECT_TEXT_LIMIT = 30


class NoteSubject:
    """ "Note 1234 (食べる)": the note's id and its sort field, read only when an error names it.
    Reading it for every note would cost a note type lookup per task of a clean run."""

    __slots__ = ("note",)

    def __init__(self, note: Any) -> None:
        self.note = note

    def __str__(self) -> str:
        note_id = getattr(self.note, "id", None)
        # A note not added yet has id 0
        label = f"Note {note_id}" if note_id else "New note"
        try:
            sort_index = self.note.note_type()["sortf"]
            text = html.unescape(_TAG.sub(" ", self.note.fields[sort_index]))
            text = " ".join(text.split())
        except Exception:
            return label
        if not text:
            return label
        if len(text) > SUBJECT_TEXT_LIMIT:
            text = text[:SUBJECT_TEXT_LIMIT].rstrip() + "…"
        return f"{label} ({text})"


@contextmanager
def error_subject(subject: Any) -> Iterator[None]:
    """Name `subject` (a `NoteSubject`, or any object that prints as one) in the errors reported
    in this context, and in the asyncio tasks created inside it."""
    token = _subject.set(subject)
    try:
        yield
    finally:
        _subject.reset(token)


def error_title(where: str = "") -> str:
    """The chain step, the task's subject and `where`, those that are known."""
    subject = _subject.get()
    parts = [_step, str(subject) if subject is not None else "", where]
    return TITLE_JOIN.join(p for p in parts if p) or "Error"


def exception_text(error: BaseException, what: str = "") -> str:
    """ "<what>: <Type>: <message>", then the traceback when the error was raised."""
    detail = str(error)
    text = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    if what:
        text = f"{what}: {text}"
    if error.__traceback__ is not None:
        # The three-argument form: the one-argument one is 3.10+, and Anki runs on 3.9
        text += "\n\n" + "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    return text


# --- Reporting ----------------------------------------------------------------------------

_deliver: Optional[Callable[[str, str], None]] = None


def deliver_with(deliver: Optional[Callable[[str, str], None]]) -> None:
    """Set what shows a reported error: `deliver(title, text)`, called on the reporting thread."""
    global _deliver
    _deliver = deliver


def report_error(text: str, where: str = "") -> None:
    """Report an error that does not fail the run, from any thread. Titled by `error_title`.
    Never raises: the caller is already handling a failure."""
    try:
        title = error_title(where)
        deliver = _deliver
        if deliver is not None:
            deliver(title, text)
    except Exception as e:
        logger.error("Could not report an error of the run: %s", e)
