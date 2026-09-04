from __future__ import annotations

from typing import List

from anki.utils import ids2str
from aqt import mw
from aqt.gui_hooks import sync_did_finish, sync_will_start
from aqt.utils import tooltip

from .configuration import Config
from .logic import run_sync_disperse_in_background
from .shared.anki.sync_hook_base import create_comparelog, review_cid_remote


def _existing_card_ids(card_ids: List[int]) -> List[int]:
    """Drop the ids whose card no longer exists.

    review_cid_remote reads the revlog, and revlog rows outlive the cards they
    belong to: a card reviewed and then deleted on another device still comes
    back from it. Resolving such an id with get_card raises NotFoundError, so
    resolve them against the cards table instead.
    """
    if not card_ids:
        return []
    assert mw.col.db is not None
    return list(mw.col.db.list(f"SELECT id FROM cards WHERE id IN {ids2str(card_ids)}"))


def _on_sync_start(local_rids: List[int]) -> None:
    local_rids.clear()
    create_comparelog(local_rids)


def _on_sync_finish(local_rids: List[int]) -> None:
    remote_reviewed_cids = _existing_card_ids(review_cid_remote(local_rids))
    if not remote_reviewed_cids:
        return

    config = Config()
    config.load()

    def show_result(messages: List[str]) -> None:
        if not messages:
            return
        # Showing the tooltip right as the op finishes gets it closed again by
        # the progress dialog still tearing down, so let that settle first.
        mw.progress.single_shot(
            100,
            lambda: tooltip(
                "<br><br>".join(messages),
                parent=mw,
                period=10000,
                y_offset=200,
            ),
        )

    run_sync_disperse_in_background(remote_reviewed_cids, config, show_result)


def init_sync_hook() -> None:
    local_rids: list[int] = []
    sync_will_start.append(lambda: _on_sync_start(local_rids))
    sync_did_finish.append(lambda: _on_sync_finish(local_rids))
