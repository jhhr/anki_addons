from __future__ import annotations

from typing import List

from aqt import mw
from aqt.gui_hooks import sync_did_finish, sync_will_start
from aqt.utils import tooltip

from .configuration import Config
from .logic import run_sync_grouped
from .shared.anki.sync_hook_base import create_comparelog, review_cid_remote


def _on_sync_start(local_rids: List[int]) -> None:
    create_comparelog(local_rids)


def _on_sync_finish(local_rids: List[int]) -> None:
    config = Config()
    config.load()

    remote_reviewed_cids = review_cid_remote(local_rids)
    if not remote_reviewed_cids:
        return

    cards = [mw.col.get_card(cid) for cid in remote_reviewed_cids]
    messages = run_sync_grouped(cards, config)
    if messages:
        tooltip("<br><br>".join(messages), period=10000)


def init_sync_hook() -> None:
    local_rids: list[int] = []
    sync_will_start.append(lambda: _on_sync_start(local_rids))
    sync_did_finish.append(lambda: _on_sync_finish(local_rids))
