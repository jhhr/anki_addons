"""Asking the user about the word array generator's first-use downloads.

`word_array/resources.py` knows what the generator needs and how to fetch it, but it is
deliberately aqt-free so the research scripts can import it. This is the other half: the
add-on side that asks before downloading ~83 MB and runs the fetch off the main thread.

Every op that runs the generator goes through `with_generator_resources`, so the question is
asked once, before any note is touched, rather than in the middle of a bulk run.
"""

import logging
from typing import Any, Callable, Optional

from anki.collection import Collection
from aqt.errors import show_exception
from aqt.operations import QueryOp
from aqt.utils import askUser, showWarning

from .async_api_ops.chain_types import ChainStep, fail_step
from .word_array import resources

logger = logging.getLogger(__name__)


def with_generator_resources(
    parent: Any, then: Callable[[], Any], chain: Optional[ChainStep] = None
) -> None:
    """Run `then` once the generator can run: SudachiPy present, and the dictionaries it needs
    downloaded, asking the user before the download.

    With `chain`, `then` is a step of a chain, and every way of not running it fails the step:
    the chain would otherwise wait for an `on_done` that never comes. The chain asks about the
    downloads once before its first step, so a step normally finds them present.
    """
    if not resources.has_sudachipy():
        showWarning(
            "The word array generator needs SudachiPy, which is missing from this add-on's"
            " libraries. Reinstall the add-on to get it."
        )
        fail_step(chain, "SudachiPy is missing")
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
        fail_step(chain, "The word array resources were not downloaded")
        return

    def fetch(_col: Collection) -> None:
        resources.ensure(lambda step: logger.info(f"Word array resources: {step}"))

    def on_fetch_failed(error: Exception) -> None:
        # Given a failure handler, aqt no longer shows the error itself
        show_exception(parent=parent, exception=error)
        fail_step(chain, f"Downloading the word array resources failed: {error}")

    fetch_op = QueryOp(parent=parent, op=fetch, success=lambda _: then())
    # Only for a chain, so a run from the menu keeps aqt's own error display untouched
    if chain is not None:
        fetch_op = fetch_op.failure(on_fetch_failed)
    fetch_op.with_progress(
        "Downloading the word array generator's dictionaries"
    ).run_in_background()
