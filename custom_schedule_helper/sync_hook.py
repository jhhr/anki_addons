from typing import List

from aqt.gui_hooks import sync_will_start, sync_did_finish

from .configuration import Config
from .ease.auto_ease_factor import adjust_ease
from .schedule.reschedule import reschedule
from .shared.anki.sync_hook_base import create_comparelog, review_cid_remote


def start_sync(local_rids: List[int]) -> None:
    create_comparelog(local_rids)


def collect_remote_reviews(
    remote_reviewed_cids: List[int], local_rids: List[int]
) -> None:
    remote_reviewed_cids.clear()
    remote_reviewed_cids.extend(review_cid_remote(local_rids))


def auto_reschedule(remote_reviewed_cids: List[int]):
    if len(remote_reviewed_cids) == 0:
        return
    config = Config()
    config.load()
    if not config.auto_reschedule_after_sync:
        return

    fut = reschedule(
        None,
        recent=False,
        filter_flag=True,
        filtered_cids=set(remote_reviewed_cids),
        notify_group="sync",
    )

    if fut:
        # wait for reschedule to finish, it posts its own result when done
        fut.result()


def auto_adjust_ease(remote_reviewed_cids: List[int]):
    if len(remote_reviewed_cids) == 0:
        return

    fut = adjust_ease(
        recent=False,
        marked_only=True,
        card_ids=set(remote_reviewed_cids),
    )

    if fut:
        # wait for adjustment to finish
        fut.result()


def init_sync_hook():
    local_rids = []
    remote_reviewed_cids = []

    sync_will_start.append(lambda: start_sync(local_rids))
    sync_did_finish.append(
        lambda: collect_remote_reviews(remote_reviewed_cids, local_rids)
    )

    # sync_did_finish.append(lambda: auto_adjust_ease(remote_reviewed_cids))
    sync_did_finish.append(lambda: auto_reschedule(remote_reviewed_cids))
