"""Ask before rebuilding an addon's vendored packages, and run it where the user can see it.

Split deliberately from the check, which is two string comparisons and a small JSON read and
runs at import time in the addon's `__init__` (see `vendor_path.vendor_health`). Everything
here needs a main window, so it waits for `main_window_did_init`. By then the addon's own
modules have already imported against the stale tree, which does not matter: a rebuild needs a
restart either way.

Three rules this follows, in the order they matter:

* **Ask first.** Nothing downloads or installs without a dialog naming the reason, the size
  and the wait. An addon that fetches and executes code silently is also the thing AnkiWeb
  review objects to, and rightly.
* **Never hard-fail.** Every dependency here degrades on its own - psutil missing falls back
  to a static concurrency limit, rapidfuzz has a complete Python implementation - so a machine
  that cannot rebuild (offline, old Anki, a proxy pip cannot use) must still run the addon.
* **Do not nag.** A refusal or a failure is recorded against the Python and addon version it
  was for, and the question does not come back until one of those changes. The menu action
  stays available in the meantime, and is worth having even on a healthy install: a local
  rebuild gets rapidfuzz's C extensions, which five platforms' worth of them in the shipped
  tree could never afford.
"""

from __future__ import annotations

import logging
from typing import Optional

from aqt import gui_hooks, mw
from aqt.qt import QAction, qconnect
from aqt.utils import askUser, showInfo, showWarning

from .vendor_rebuild import (
    can_rebuild,
    clear_attempts,
    prompt_is_due,
    rebuild_libs,
    record_attempt,
)

logger = logging.getLogger(__name__)
# Anki turns anything written to stderr into an "an error occurred" report, and logging's
# last-resort handler writes there whenever a record finds no handler anywhere up the chain -
# which is exactly the state at startup, before the addon attaches a file handler of its own.
# Everything logged in this module already has a dialog, so the stderr copy was a second,
# scarier report of something that had been handled. A NullHandler is enough to stop it, and
# records still reach the addon's real handlers whenever it has any.
logger.addHandler(logging.NullHandler())

_PROGRESS_LABEL = "Rebuilding helper packages..."


def install_rebuild_ui(addon_dir: str, addon_name: str, health: Optional[str]) -> None:
    """Wire up the on-demand menu action, and the startup offer when `health` is a reason.

    `health` is whatever `vendor_path.vendor_health` returned at import time - None when the
    vendored tree fits this machine.
    """

    def on_main_window_did_init() -> None:
        _add_menu_action(addon_dir, addon_name)
        if health is None:
            return
        logger.warning("vendored lib does not fit this machine: %s", health)
        if not prompt_is_due(addon_dir):
            logger.info("not offering a rebuild again for this Python and addon version")
            return
        blocked = can_rebuild()
        if blocked:
            # Nothing to offer, so nothing to say. Recorded so the pip probe does not run
            # again at every startup on a machine where the answer cannot change.
            logger.warning("cannot rebuild here: %s", blocked)
            record_attempt(addon_dir, f"unavailable: {blocked}")
            return
        if not askUser(_offer_text(addon_name, health), title=addon_name, defaultno=False):
            record_attempt(addon_dir, "declined")
            return
        _run_rebuild(addon_dir, addon_name)

    gui_hooks.main_window_did_init.append(on_main_window_did_init)


def _add_menu_action(addon_dir: str, addon_name: str) -> None:
    action = QAction(f"Rebuild {addon_name} helper packages...", mw)
    qconnect(action.triggered, lambda: _rebuild_on_demand(addon_dir, addon_name))
    menu = getattr(getattr(mw, "form", None), "menuTools", None)
    if menu is None:
        logger.warning("no Tools menu to add the rebuild action to")
        return
    menu.addAction(action)


def _rebuild_on_demand(addon_dir: str, addon_name: str) -> None:
    """The menu action. The user asked, so an obstacle is worth saying out loud."""
    blocked = can_rebuild()
    if blocked:
        showWarning(
            f"{addon_name} cannot rebuild its helper packages here, because {blocked}.\n\n"
            "The add-on will keep working with the packages it shipped with.",
            title=addon_name,
        )
        return
    if not askUser(_offer_text(addon_name, None), title=addon_name, defaultno=False):
        return
    _run_rebuild(addon_dir, addon_name)


def _offer_text(addon_name: str, health: Optional[str]) -> str:
    if health:
        opening = (
            f"<b>{addon_name}</b> ships pre-built helper packages, and the ones it shipped do"
            f" not fit this copy of Anki: {health}.<br><br>"
            "It can rebuild them for this machine now."
        )
    else:
        opening = (
            f"<b>{addon_name}</b> can rebuild its helper packages for this machine. The ones"
            " it ships have to work on five different platforms, so some of them leave out"
            " their compiled half; a rebuild here does not have to, and word matching gets"
            " noticeably faster."
        )
    return (
        f"{opening}<br><br>"
        "This downloads around 10 MB from PyPI, at the exact versions listed in the add-on's"
        " <code>requirements.txt</code>, and installs them inside the add-on's"
        " <code>user_files</code> folder. Nothing else on the system is touched. It usually"
        " takes well under a minute, Anki cannot be used while it runs, and it needs a restart"
        " afterwards.<br><br>"
        "The add-on works either way - just more slowly without this.<br><br>"
        "Rebuild now?"
    )


def _run_rebuild(addon_dir: str, addon_name: str) -> None:
    """Blocking, on purpose: using Anki while its packages are being replaced is asking for it.

    with_progress puts the subprocess on a background thread behind a modal dialog, so Qt keeps
    painting. A frozen unpainted window for the ten to thirty seconds this takes would be
    indistinguishable from a hang.
    """

    def on_progress(message: str) -> None:
        mw.taskman.run_on_main(lambda: mw.progress.update(label=message))

    def task() -> None:
        rebuild_libs(addon_dir, on_progress)

    def on_done(future) -> None:
        try:
            future.result()
        except Exception as error:
            logger.error("rebuild failed: %s", error, exc_info=True)
            record_attempt(addon_dir, "failed")
            showWarning(
                f"{addon_name} could not rebuild its helper packages:\n\n{error}\n\n"
                "The add-on will keep working with the packages it shipped with. You can try"
                " again from Tools.",
                title=addon_name,
            )
            return
        clear_attempts(addon_dir)
        showInfo(
            f"{addon_name} rebuilt its helper packages for this machine.\n\n"
            "Restart Anki to start using them.",
            title=addon_name,
        )

    mw.taskman.with_progress(
        task,
        on_done,
        label=_PROGRESS_LABEL,
        title=addon_name,
        # It runs pip against a directory inside the addon and never opens the collection.
        uses_collection=False,
    )
