"""Revlog scaffolding for telling apart locally and remotely reviewed cards.

`create_comparelog` records the revlog ids present before a sync starts;
`review_cid_remote` then reports the cards whose reviews arrived during it. Both
were duplicated in copy_anywhere and custom_schedule_helper with identical SQL --
only the sync actions built on top of them differ, and those stay in each addon.

Usage:

    local_rids: list[int] = []
    sync_will_start.append(lambda: create_comparelog(local_rids))
    sync_did_finish.append(lambda: do_something(review_cid_remote(local_rids)))
"""

from typing import List

from anki.utils import ids2str
from aqt import mw


def create_comparelog(local_rids: List[int]) -> None:
    """Snapshot the revlog ids that exist locally, before a sync brings more in."""
    assert mw.col.db is not None
    local_rids.clear()
    local_rids.extend(mw.col.db.list("SELECT id FROM revlog"))


def review_cid_remote(local_rids: List[int]) -> List[int]:
    """Cards reviewed elsewhere: revlog entries absent from the pre-sync snapshot.

    Manual entries (type 4) are excluded; 0=learning, 1=review, 2=relearn,
    3=filtered are all real reviews.
    """
    assert mw.col.db is not None
    local_rid_string = ids2str(local_rids)
    return list(
        mw.col.db.list(f"""SELECT DISTINCT cid
            FROM revlog
            WHERE id NOT IN {local_rid_string}
            AND type < 4
            """)
    )
