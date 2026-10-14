"""Where the add-on's commands report: the shared notification stack.

A tooltip replaces whatever another add-on had just shown, and is gone in seconds. Every
command here posts to the stack instead, under one name, and keyed by the command: running
one again replaces its last report rather than stacking a second beside it.
"""

from __future__ import annotations

import time

from .shared.notify import post

NOTIFY_SOURCE = "Custom Schedule Helper"


def report_done(title: str, started: float, key: str, group: str = "") -> None:
    """A command that ran: what it did, and how long that took.

    `group` batches the report with whatever else posted in that group; the sync hook's
    reschedule passes "sync".
    """
    post(
        source=NOTIFY_SOURCE,
        title=title,
        body=f"Finished in {time.time() - started:.2f} seconds",
        level="success",
        group=group,
        key=key,
    )


def report_refused(title: str, key: str) -> None:
    """A command that did not start, and why."""
    post(source=NOTIFY_SOURCE, title=title, level="warning", key=key)
