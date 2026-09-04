from typing import List

from anki.utils import ids2str
from aqt import mw
from aqt.gui_hooks import sync_will_start, sync_did_finish
from aqt.utils import tooltip

from .configuration import Config
from .ease.auto_ease_factor import adjust_ease
from .schedule.disperse_siblings import disperse_siblings
from .schedule.reschedule import reschedule


def create_comparelog(local_rids: List[int], texts: List[str]) -> None:
    texts.clear()
    local_rids.clear()
    local_rids.extend([id for id in mw.col.db.list("SELECT id FROM revlog")])


def review_cid_remote(remote_reviewed_cids: List[int], local_rids: List[int]):
    local_rid_string = ids2str(local_rids)
    remote_reviewed_cids.extend(
        [
            cid for cid in mw.col.db.list(f"""SELECT DISTINCT cid
            FROM revlog
            WHERE id NOT IN {local_rid_string}
            AND type < 4
            """)  # type: 0=Learning, 1=Review, 2=relearn, 3=filtered, 4=Manual
        ]
    )


def auto_reschedule(remote_reviewed_cids: List[int], texts: List[str]):
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
    )

    if fut:
        # wait for reschedule to finish
        (reschedule_result_msg, _) = fut.result()
        texts.append(reschedule_result_msg)


def auto_disperse(remote_reviewed_cids: List[int], texts: List[str]):
    if len(remote_reviewed_cids) == 0:
        return
    config = Config()
    config.load()
    if not config.auto_disperse_after_sync:
        return

    remote_reviewed_cid_string = ids2str(remote_reviewed_cids)
    remote_reviewed_nids = [nid for nid in mw.col.db.list(f"""SELECT DISTINCT nid 
            FROM cards 
            WHERE id IN {remote_reviewed_cid_string}
        """)]
    remote_reviewed_nid_string = ids2str(remote_reviewed_nids)

    fut = disperse_siblings(
        None,
        filter_flag=True,
        filtered_nid_string=remote_reviewed_nid_string,
        text_from_reschedule="<br>".join(texts),
    )

    if fut:
        # Disperse siblings is the last operation, so we can show the result now
        # Instead of returning the future, we show a tooltip with the result so
        # we can set our own period
        tooltip(
            fut.result(),
            period=10000,
        )


def auto_adjust_ease(remote_reviewed_cids: List[int], texts: List[str]):
    if len(remote_reviewed_cids) == 0:
        return

    fut = adjust_ease(
        recent=False,
        marked_only=True,
        card_ids=set(remote_reviewed_cids),
    )

    if fut:
        # wait for adjustment to finish
        texts.append(fut.result())


def init_sync_hook():
    local_rids = []
    remote_reviewed_cids = []
    texts = []

    sync_will_start.append(lambda: create_comparelog(local_rids, texts))
    sync_did_finish.append(lambda: review_cid_remote(remote_reviewed_cids, local_rids))

    # sync_did_finish.append(lambda: auto_adjust_ease(remote_reviewed_cids, texts))
    sync_did_finish.append(lambda: auto_reschedule(remote_reviewed_cids, texts))
    sync_did_finish.append(lambda: auto_disperse(remote_reviewed_cids, texts))
