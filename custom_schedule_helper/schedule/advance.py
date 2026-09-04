import time

from anki.decks import DeckManager
from anki.stats import (
    QUEUE_TYPE_REV,
)
from anki.utils import ids2str
from aqt import mw
from aqt.utils import tooltip, getText, showWarning

from ..utils import (
    RepresentsInt,
    get_last_review_date,
    update_card_due_ivl,
    write_custom_data,
)


def get_desired_advance_cnt_with_response(safe_cnt, did):
    inquire_text = "Enter the number of cards to be advanced.\n"
    notification_text = f"{'For this deck' if did else 'For this collection'}, it is relatively safe to advance up to {safe_cnt} cards.\n"
    warning_text = "You can advance more cards if you wish, but it is not recommended.\nKeep in mind that whenever you use Postpone or Advance, you depart from the optimal scheduling.\n"
    info_text = "This feature only affects the cards that have been scheduled by Custom Schedule."
    (s, r) = getText(
        inquire_text + notification_text + warning_text + info_text, default="10"
    )
    if r:
        return (RepresentsInt(s), r)
    return (None, r)


def advance(did):
    DM = DeckManager(mw.col)
    if did is not None:
        did_list = ids2str(DM.deck_and_child_ids(did))

    cards = mw.col.db.all(
        f"""
        SELECT 
            id, 
            CASE WHEN odid==0
            THEN did
            ELSE odid
            END,
            ivl,
            factor,
            CASE WHEN odid==0
            THEN {mw.col.sched.today} - (due - ivl)
            ELSE {mw.col.sched.today} - (odue - ivl)
            END
        FROM cards
        WHERE due > {mw.col.sched.today}
        AND queue = {QUEUE_TYPE_REV}
        {"AND did IN %s" % did_list if did is not None else ""}
    """
    )
    # x[0]: cid
    # x[1]: did
    # x[2]: factor
    # x[3]: interval
    # x[4]: elapsed days

    # sort by (elapsed_days / interval - 1), -interval (ascending)
    cards = sorted(cards, key=lambda x: (x[4] / x[3] - 1, -x[3]))
    safe_cnt = len(list(filter(lambda x: x[4] / x[3] - 1 - 1 < 0.15, cards)))

    (desired_advance_cnt, resp) = get_desired_advance_cnt_with_response(safe_cnt, did)
    if desired_advance_cnt is None:
        if resp:
            showWarning("Please enter the number of cards you want to advance.")
        return
    else:
        if desired_advance_cnt <= 0:
            showWarning("Please enter a positive integer.")
            return

    undo_entry = mw.col.add_custom_undo_entry("Advance")

    mw.progress.start()
    start_time = time.time()

    cnt = 0
    for cid, _, _, _, _ in cards:
        if cnt >= desired_advance_cnt:
            break

        card = mw.col.get_card(cid)
        last_review = get_last_review_date(card)
        new_ivl = mw.col.sched.today - last_review
        card = update_card_due_ivl(card, new_ivl)
        write_custom_data(card, "v", "a")
        mw.col.update_card(card)
        mw.col.merge_undo_entries(undo_entry)
        cnt += 1

    tooltip(f"""{cnt} cards advanced in {time.time() - start_time:.2f} seconds.""")
    mw.progress.finish()
    mw.reset()
