# inspired by https://eshapard.github.io/
import math
import time

from anki.consts import (
    REVLOG_LRN,
    REVLOG_REV,
    REVLOG_RELRN,
    REVLOG_CRAM,
    QUEUE_TYPE_DAY_LEARN_RELEARN,
    QUEUE_TYPE_LRN,
    QUEUE_TYPE_REV,
)
from anki.decks import DeckManager
from anki.utils import ids2str
from aqt import mw

# anki interfaces
from aqt import reviewer
from aqt.utils import tooltip

from ..configuration import Config
from ..utils import write_custom_data

LOG = True

# add on utilities
from .ease_calculator import calculate_ease, get_success_rate, moving_average


def get_all_reps(card=mw.reviewer.card) -> list[int]:
    return mw.col.db.list(f"""
        select ease
        from revlog
        where cid = {card.id}
        and type IN ({REVLOG_LRN}, {REVLOG_REV}, {REVLOG_RELRN}, {REVLOG_CRAM})
        """)


def get_all_reps_with_ids(card=mw.reviewer.card) -> list[tuple[int, int]]:
    return mw.col.db.all(f"""
        select id, ease
        from revlog
        where cid = {card.id}
        and type IN ({REVLOG_LRN}, {REVLOG_REV}, {REVLOG_RELRN}, {REVLOG_CRAM})
        """)


def get_reviews_only(card=mw.reviewer.card, rids: list[int] = None) -> list[int]:
    return mw.col.db.list(f"""
        select ease
        from revlog
        where type = {REVLOG_REV}
        and cid = {card.id}
        {f"and id IN {ids2str(rids)}" if rids else ""}
        """)


def get_ease_factors(card=mw.reviewer.card) -> list[int]:
    return mw.col.db.list(f"""
        select factor
        from revlog
        where cid = {card.id}
        and factor > 0
        and type IN ({REVLOG_LRN}, {REVLOG_REV}, {REVLOG_RELRN}, {REVLOG_CRAM})
""")


def get_starting_ease(card=mw.reviewer.card) -> int:
    deck_id = card.did
    if card.odid:
        deck_id = card.odid
    try:
        deck_starting_ease = mw.col.decks.config_dict_for_deck_id(deck_id)["new"]["initialFactor"]
    except KeyError:
        deck_starting_ease = 2500
    return deck_starting_ease


def suggested_factor(
    config,
    card=mw.reviewer.card,
    new_answer=None,
    prev_card_factor=None,
    leashed=True,
    is_deck_adjustment=False,
    set_custom_data=True,
) -> int:
    """Loads card history from anki and returns suggested factor"""

    deck_starting_ease = get_starting_ease(card)

    # Wraps calculate_ease()
    card_settings = {}
    card_settings["id"] = card.id
    card_settings["is_review_card"] = card.type == 2
    # If doing deck adjustment, rewrite all past factors in revlog
    if is_deck_adjustment:
        all_reps = get_all_reps_with_ids(card)
        card_settings["factor_list"] = [deck_starting_ease]
        for i in range(len(all_reps)):
            rep_id = all_reps[i][0]
            card_settings["review_list"] = (
                get_reviews_only(card, rids=[r[0] for r in all_reps[0:i]])
                if config.reviews_only
                else [r[1] for r in all_reps[0:i]]
            )
            new_factor, _ = calculate_ease(config, deck_starting_ease, card_settings, leashed)
            # This breaks undo history, so no undoing is possible when doing deck adjustment
            mw.col.db.execute("update revlog set factor = ? where id = ?", new_factor, rep_id)
            card_settings["factor_list"].append(new_factor)
    if config.reviews_only:
        card_settings["review_list"] = get_reviews_only(card)
    else:
        card_settings["review_list"] = get_all_reps(card)
    if new_answer is not None:
        append_answer = new_answer
        card_settings["review_list"].append(append_answer)
    factor_list = get_ease_factors(card)
    if factor_list is not None and len(factor_list) > 0 and prev_card_factor is not None:
        factor_list[-1] = prev_card_factor
    card_settings["factor_list"] = factor_list
    # Ignore latest ease if you are applying algorithm from deck settings
    if new_answer is None and len(card_settings["factor_list"]) > 1:
        card_settings["factor_list"] = card_settings["factor_list"][:-1]
    new_factor, success_rate = calculate_ease(
        config=config,
        deck_starting_ease=deck_starting_ease,
        card_settings=card_settings,
        leashed=leashed,
    )
    if set_custom_data:
        write_custom_data(
            card,
            key_values=[
                {"key": "e", "value": "a"},
                {"key": "sr", "value": round(success_rate, 3)},
            ],
        )
    return new_factor


def get_stats(config, card=mw.reviewer.card, new_answer=None, prev_card_factor=None):
    rep_list = get_all_reps(card)
    if new_answer:
        rep_list.append(new_answer)
    factor_list = get_ease_factors(card)
    weight = config.moving_average_weight
    target = config.target_ratio
    starting_ease_factor = get_starting_ease(card)

    if rep_list is None or len(rep_list) < 1:
        success_rate = target
    else:
        success_rate = get_success_rate(rep_list, weight, init=target)
    if factor_list and len(factor_list) > 0:
        average_ease = moving_average(factor_list, weight)
    else:
        average_ease = starting_ease_factor

    # add last review (maybe simplify by doing this after new factor applied)
    printable_rep_list = ""
    if len(rep_list) > 0:
        truncated_rep_list = rep_list[-10:]
        if len(rep_list) > 10:
            printable_rep_list += "..., "
        printable_rep_list += str(truncated_rep_list[0])
        for rep_result in truncated_rep_list[1:]:
            printable_rep_list += ", " + str(rep_result)
    if factor_list and len(factor_list) > 0:
        last_rev_factor = factor_list[-1]
    else:
        last_rev_factor = None
    delta_ratio = math.log(target) / math.log(success_rate)
    card_types = {0: "new", 1: "learn", 2: "review", 3: "relearn"}
    queue_types = {
        0: "new",
        1: "relearn",
        2: "review",
        3: "day (re)lrn",
        4: "preview",
        -1: "suspended",
        -2: "sibling buried",
        -3: "manually buried",
    }

    msg = f"card ID: {card.id}<br>"
    msg += f"Card Queue (Type): {queue_types[card.queue]} ({card_types[card.type]})<br>"
    msg += f"MAvg success rate: {round(success_rate, 4)}<br>"
    msg += f"MAvg factor: {round(average_ease, 2)}<br>"
    msg += f""" (delta: {round(delta_ratio, 2)})<br>"""
    if last_rev_factor == prev_card_factor:
        msg += f"Last rev factor: {last_rev_factor}<br>"
    else:
        msg += f"Last rev factor: {last_rev_factor}"
        msg += f" (actual: {prev_card_factor})<br>"

    if card.queue != 2 and config.reviews_only:
        msg += "New factor: NONREVIEW, NO CHANGE<br>"
    else:
        new_factor = suggested_factor(
            config, card, new_answer, prev_card_factor, set_custom_data=False
        )
        unleashed_factor = suggested_factor(
            config,
            card,
            new_answer,
            prev_card_factor,
            leashed=False,
            set_custom_data=False,
        )
        if new_factor == unleashed_factor:
            msg += f"New factor: {new_factor}<br>"
        else:
            msg += f"""New factor: {new_factor}"""
            msg += f""" (unleashed: {unleashed_factor})<br>"""
    msg += f"Rep list: {printable_rep_list}<br>"
    return msg


def display_stats(config, new_answer=None, prev_card_factor=None):
    card = mw.reviewer.card
    msg = get_stats(config, card, new_answer, prev_card_factor)
    tooltip_args = {"msg": msg, "period": config.stats_duration}
    tooltip(**tooltip_args)


def adjust_factor_when_review(ease_tuple, reviewer=reviewer.Reviewer, card=mw.reviewer.card):
    config = Config()
    config.load()

    if not config.auto_adjust_ease_on_review:
        return ease_tuple
    assert card is not None
    new_answer = ease_tuple[1]
    prev_card_factor = card.factor
    if card.queue == 2 or not config.reviews_only:
        card.factor = suggested_factor(
            config=config,
            card=card,
            new_answer=new_answer,
            prev_card_factor=prev_card_factor,
        )
    if config.stats_enabled:
        display_stats(config, new_answer, prev_card_factor)
    return ease_tuple


def adjust_factor_after_review(reviewer: reviewer.Reviewer, card: mw.reviewer.card, ease: int):
    config = Config()
    config.load()

    if not config.auto_adjust_ease_after_review:
        return

    assert card is not None
    if card.queue == 2 or not config.reviews_only:
        # Merge undo entry for the review
        undo_status = mw.col.undo_status()
        undo_entry = undo_status.last_step
        card.factor = suggested_factor(config, card)
        # Update card with the new custom_data
        mw.col.update_card(card)
        mw.col.merge_undo_entries(undo_entry)


def adjust_ease_factors_background(
    did=None,
    recent=False,
    marked_only=False,
    card_ids=None,
):
    config = Config()
    config.load()

    mw.taskman.run_on_main(lambda: mw.progress.start(label="Adjusting ease", immediate=False))

    cnt = 0
    DM = DeckManager(mw.col)

    if did is not None:
        did_list = ids2str(DM.deck_and_child_ids(did))
        did_query = f"AND did IN {did_list}"

    if recent:
        today_cutoff = mw.col.sched.day_cutoff
        day_before_cutoff = today_cutoff - (config.days_to_reschedule + 1) * 86400
        recent_query = f"AND id IN (SELECT cid FROM revlog WHERE id >= {day_before_cutoff * 1000})"

    if card_ids:
        card_ids_query = f"AND id IN {ids2str(card_ids)}"

    if marked_only:
        marked_query = "AND json_extract(json_extract(data, '$.cd'), '$.e') = 0"

    card_ids = mw.col.db.list(f"""
        SELECT
            id
        FROM cards
        WHERE queue IN ({QUEUE_TYPE_LRN}, {QUEUE_TYPE_REV}, {QUEUE_TYPE_DAY_LEARN_RELEARN})
        {did_query if did is not None else ""}
        {recent_query if recent else ""}
        {card_ids_query if card_ids else ""}
        {marked_query if marked_only else ""}
    """)

    for card_id in card_ids:
        card = mw.col.get_card(card_id)
        if LOG:
            print("old factor", card.factor)
        card.factor = suggested_factor(config=config, card=card, is_deck_adjustment=True)
        if LOG:
            print("new factor", card.factor)

        mw.col.update_card(card)
        # This is a deck adjustment, so merging undo entries is not possible due to the
        # db.execute() call in suggested_factor
        cnt += 1
        if cnt % 200 == 0:
            mw.taskman.run_on_main(
                lambda: mw.progress.update(value=cnt, label=f"{cnt} cards adjusted")
            )
        if mw.progress.want_cancel():
            break

    return f"Adjusted ease for {cnt} cards"


def adjust_ease(
    did=None,
    recent=False,
    marked_only=False,
    card_ids=None,
    parent=None,
):
    start_time = time.time()

    def on_done(future):
        mw.progress.finish()
        tooltip(f"{future.result()} in {time.time() - start_time:.2f} seconds")

    fut = mw.taskman.run_in_background(
        lambda: adjust_ease_factors_background(
            did=did,
            recent=recent,
            marked_only=marked_only,
            card_ids=card_ids,
        ),
        on_done,
    )

    return fut
