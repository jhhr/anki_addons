"""Asking the user about the word array generator's first-use downloads.

`word_array/resources.py` knows what the generator needs and how to fetch it, but it is
deliberately aqt-free so the research scripts can import it. This is the other half: the
add-on side that asks before downloading ~83 MB and runs the fetch off the main thread.

Every op that runs the generator goes through `with_generator_resources`, so the question is
asked once, before any note is touched, rather than in the middle of a bulk run.
"""

import logging
from typing import Any, Callable

from anki.collection import Collection
from aqt.operations import QueryOp
from aqt.utils import askUser, showWarning

from .word_array import resources

logger = logging.getLogger(__name__)


def with_generator_resources(parent: Any, then: Callable[[], Any]) -> None:
    """Run `then` once the generator can run: SudachiPy present, and the dictionaries it needs
    downloaded, asking the user before the download."""
    if not resources.has_sudachipy():
        showWarning(
            "The word array generator needs SudachiPy, which is missing from this add-on's"
            " libraries. Reinstall the add-on to get it."
        )
        return
    needed = resources.missing()
    if not needed:
        then()
        return

    downloads = "\n".join(f"  - {d.name}, {d.size_mb:.0f} MB" for d in needed)
    if not askUser(
        "The word array generator needs these, once:\n\n"
        f"{downloads}\n\n"
        "They are downloaded into the add-on's user_files, which Anki keeps across add-on"
        " updates. Download them now?",
        parent=parent,
        title="Word array resources",
    ):
        return

    def fetch(_col: Collection) -> None:
        resources.ensure(lambda step: logger.info(f"Word array resources: {step}"))

    QueryOp(
        parent=parent,
        op=fetch,
        success=lambda _: then(),
    ).with_progress("Downloading the word array generator's dictionaries").run_in_background()
