import time
from anki.consts import (
    REVLOG_LRN,
    REVLOG_REV,
    REVLOG_RELRN,
    REVLOG_CRAM,
    QUEUE_TYPE_SUSPENDED,
    CARD_TYPE_NEW,
)

from anki.cards import FSRSMemoryState
from anki.decks import DeckManager
from anki.utils import ids2str
from aqt.utils import tooltip
from aqt import mw

from .fsrs_calculator import FsrsCalculator, Review, StepResults
from ..configuration import Config
from ..py_fsrs.fsrs import State, Rating
from datetime import timedelta

LOG = False


def adjust_fsrs_revlog_background(
    did=None,
    recent=False,
    marked_only=False,
    query_res=None,
):
    config = Config()
    config.load()

    mw.taskman.run_on_main(
        lambda: mw.progress.start(label="Adjusting FSRS review log", immediate=False)
    )

    cnt = 0
    DM = DeckManager(mw.col)

    if did is not None:
        did_list = ids2str(DM.deck_and_child_ids(did))
        did_query = f"AND did IN {did_list}"

    if recent:
        today_cutoff = mw.col.sched.day_cutoff
        day_before_cutoff = today_cutoff - (config.days_to_reschedule + 1) * 86400
        recent_query = f"AND id IN (SELECT cid FROM revlog WHERE id >= {day_before_cutoff * 1000})"

    if query_res:
        card_ids_query = f"AND id IN {ids2str(query_res)}"

    if marked_only:
        marked_query = "AND json_extract(json_extract(data, '$.cd'), '$.e') = 0"

    query = f"""
        SELECT
            id, did, odid
        FROM cards
        WHERE queue != {QUEUE_TYPE_SUSPENDED}
        AND type != {CARD_TYPE_NEW}
        {did_query if did is not None else ""}
        {recent_query if recent else ""}
        {card_ids_query if query_res else ""}
        {marked_query if marked_only else ""}
    """

    query_res = mw.col.db.all(query)

    if LOG:
        print(f"Found {len(query_res)} cards with query {query} to adjust FSRS revlog for")

    # group cards by did to process them each with one FSRS calculator instance
    card_ids_by_did = {}
    for card_id, did, odid in query_res:
        deck_id = odid or did
        if deck_id not in card_ids_by_did:
            card_ids_by_did[deck_id] = []
        card_ids_by_did[deck_id].append(card_id)

    if LOG:
        print(f"Grouped cards into {len(card_ids_by_did)} decks for processing")

    # Loop through deck_ids
    for deck_id, card_ids in card_ids_by_did.items():
        if LOG:
            print(f"Processing deck {deck_id} with {len(query_res)} cards")
        # Get deck FSRS parameters
        deck = mw.col.decks.get(deck_id)
        if deck is None:
            print(f"Deck with id {deck_id} not found, skipping")
            continue
        config_dict = mw.col.decks.config_dict_for_deck_id(deck["id"])
        fsrs4_weights = config_dict.get("fsrsWeights", None)
        fsrs5_params = config_dict.get("fsrsParams5", None)
        fsrs_params = fsrs5_params or fsrs4_weights
        if fsrs_params is None:
            print(f"Deck {deck['name']} does not have FSRS parameters, skipping")
            continue
        desired_retention = config_dict.get("desiredRetention", 0.9)
        maximum_interval = config_dict.get("maximumInterval", 3650)
        enable_fuzzing = config_dict.get("fuzz") is not None and config_dict["fuzz"] > 0
        learning_steps = config_dict.get("delays", None)
        lapse_config = config_dict.get("lapse", None)
        if lapse_config is not None:
            relearning_steps = lapse_config.get("delays", None)

        # Create a new FSRS calculator instance for each deck
        # Learning steps are stored in minutes in the collection
        def minutes_to_timedeltas(minute_count):
            if minute_count is None:
                return None
            return [timedelta(minutes=step) for step in minute_count]

        fsrs_calculator = FsrsCalculator(
            parameters=fsrs_params,
            desired_retention=desired_retention,
            maximum_interval=maximum_interval,
            enable_fuzzing=enable_fuzzing,
            learning_steps=minutes_to_timedeltas(learning_steps),
            relearning_steps=minutes_to_timedeltas(relearning_steps),
        )
        for card_id in card_ids:
            card = mw.col.get_card(card_id)
            # With FSRS, no need to edit the memory_state, we just change the factors
            # stored in revlog
            revs = mw.col.db.all(f"""
                    SELECT
                        id, type, ease
                    FROM revlog
                    WHERE cid = {card_id}
                    AND type IN ({REVLOG_LRN}, {REVLOG_REV}, {REVLOG_RELRN}, {REVLOG_CRAM})
                    ORDER BY id ASC
                    """)
            if LOG:
                print(f"Found {len(revs)} reviews for card {card_id} in deck {deck['name']}")
            reviews = []
            for revlog_id, rev_type, ease in revs:
                if rev_type == REVLOG_LRN:
                    state = State.Learning
                elif rev_type == REVLOG_REV:
                    state = State.Review
                elif rev_type == REVLOG_RELRN:
                    state = State.Relearning
                elif rev_type == REVLOG_CRAM:
                    # Review in filtered deck
                    state = State.Review

                reviews.append(
                    Review(
                        rating=Rating(ease),  # Convert ease to Rating enum
                        revlog_id=revlog_id,
                        state=state,
                    )
                )
            step_results: StepResults = fsrs_calculator.steps(
                card_id=card_id,
                reviews=reviews,
            )

            # This is a deck adjustment, so merging undo entries is not possible due to the
            # db.execute()
            for step_result in step_results:
                mw.col.db.execute(
                    "UPDATE revlog SET factor = ? WHERE id = ?",
                    int(step_result["factor"]),
                    int(step_result["revlog_id"]),
                )
            # Set the card's difficulty and stability based on the last review
            if step_results:
                last_step_result = step_results[-1]
                computed_memory_state = mw.col.compute_memory_state(card_id)
                new_memory_state = FSRSMemoryState(
                    # Set difficulty from the last step result
                    difficulty=last_step_result["difficulty"],
                    # But keep normal stability
                    stability=computed_memory_state.stability,
                )
                card.memory_state = new_memory_state
            mw.col.update_card(card)
            cnt += 1
            if cnt % 5 == 0:
                mw.taskman.run_on_main(
                    lambda: mw.progress.update(value=cnt, label=f"{cnt} cards' revlogs adjusted")
                )
            if mw.progress.want_cancel():
                break

    return f"Adjusted revlog for {cnt} cards"


def adjust_fsrs_revlog(
    did=None,
    recent=False,
    marked_only=False,
    card_ids=None,
    parent=None,
):
    """Adjust FSRS review log for cards in the collection."""
    start_time = time.time()

    if LOG:
        print(
            f"Adjusting FSRS review log for did={did}, recent={recent}, marked_only={marked_only},"
            f" card_ids={card_ids}"
        )

    def on_done(future):
        mw.progress.finish()
        tooltip(f"{future.result()} in {time.time() - start_time:.2f} seconds")

    fut = mw.taskman.run_in_background(
        lambda: adjust_fsrs_revlog_background(
            did=did,
            recent=recent,
            marked_only=marked_only,
            query_res=card_ids,
        ),
        on_done,
    )

    return fut
