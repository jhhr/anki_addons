"""Word matching judge: the words of a note's word array judged one request each.

The judge decides which words of a word array get a note, in `match_data` (see
`word_array.match_flags`). Which words are asked about is the mode, a set of `MatchState`s:

- `JUDGE_NEW` - only unjudged words, `[]`. The default; nothing already decided is touched.
- `REJUDGE_MATCHED` - words with a note id, `[id]` and `[id, quality]`. A dontmatch unlinks it.
- `REJUDGE_ALL` - every judged word, states 2-5.

Every word is asked about alone, under the rules for its part of speech (`word_array.judge`),
so a note fans out into as many requests as it has words and runs through `bulk_nested_notes_op`.
A note whose field still holds an old extract_words word list is skipped, and note ids a re-judge
unlinked are logged, so a link lost to a bad call can be put back. Particles and the
copula are judged `dontmatch` while planning, without a request. Once a note's requests are done
the array is written back, keeping every word that got a decision; a word whose request failed
stays as it was for the next run.
"""

import asyncio
import logging
from collections.abc import Sequence
from functools import partial
from typing import Optional

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning

from ..utils import get_field_config, print_error_traceback
from ..word_array import judge
from ..word_array.match_flags import (
    JUDGE_NEW,
    REJUDGE_ALL,
    REJUDGE_MATCHED,
    MatchState,
    decode_word_array,
    format_word_array,
)
from .base_ops import (
    AsyncTaskProgressUpdater,
    CancelState,
    NotePlan,
    bulk_nested_notes_op,
    get_response,
    make_inner_bulk_op,
    selected_notes_op,
)
from .concurrency import ConcurrencyGate

logger = logging.getLogger(__name__)

MODE_NAMES = {
    JUDGE_NEW: "Judging words matchability",
    REJUDGE_MATCHED: "Re-judging matched words",
    REJUDGE_ALL: "Re-judging matched/judged words",
}


def judge_model(config: dict) -> str:
    return config.get("word_matching_judge_model") or config.get("extract_words_model", "")


def plan_word_matching_judge(
    config: dict,
    note: Note,
    edited_nids: list[NoteId],
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    progress_updater: AsyncTaskProgressUpdater,
    cancel_state: CancelState,
    gate: ConcurrencyGate,
    states: frozenset[MatchState] = JUDGE_NEW,
) -> Optional[NotePlan]:
    """Plan judging the words of a note's word array in `states`, a task per word that needs a
    request. Returns None, with the note counted done, when no request is needed; a note whose
    only judgements were made while planning is saved right away."""
    log_prefix = f"Word matching judge--nid:{note.id}--"
    plan = None
    word_list_field = None
    note_type = note.note_type()
    try:
        if note_type:
            word_list_field = get_field_config(config, "word_list_field", note_type)
    except Exception as e:
        logger.error(f"{log_prefix}{e}")
    arr = (
        decode_word_array(note[word_list_field])
        if word_list_field and word_list_field in note
        else None
    )
    if arr is None:
        logger.debug(f"{log_prefix}The word list field holds no word array")
    else:
        try:
            plan = judge.plan_judgements(arr, states)
        except ValueError as e:
            # match_state on match_data it doesn't know: leave the note for a look
            logger.error(f"{log_prefix}{e}")
    if arr is None or word_list_field is None or plan is None or not (plan.auto or plan.asks):
        progress_updater.increment_counts(notes_done=1)
        return None

    for note_id in judge.set_auto(plan):
        logger.warning(f"{log_prefix}unlinked note {note_id} from a particle or copula")

    def save_note():
        current_note = notes_to_update_dict.get(note.id, note)
        current_note[word_list_field] = format_word_array(arr)
        if current_note.id > 0:
            notes_to_update_dict[current_note.id] = current_note
            edited_nids.append(current_note.id)

    if not plan.asks:
        save_note()
        progress_updater.increment_counts(notes_done=1)
        return None

    model = judge_model(config)
    judged = [False] * len(plan.asks)

    def judge_op(
        _config: dict,
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
        ask_index: int,
    ) -> bool:
        ask = plan.asks[ask_index]
        response = get_response(
            model,
            ask.prompt,
            cancel_state=cancel_state,
            response_schema=judge.RESPONSE_SCHEMA,
        )
        if response is None:
            logger.error(f"{log_prefix}No response from the judge for {ask.elem[2]}")
            return False
        note_id = judge.apply_word_response(ask.elem, response)
        logger.debug(f"{log_prefix}{ask.elem[2]}/{ask.elem[3]} ({ask.group}): {response}")
        if note_id is not None:
            logger.warning(f"{log_prefix}unlinked note {note_id} from {ask.elem[2]}")
        judged[ask_index] = True
        return True

    def make_error_handler(ask: judge.WordAsk):
        def handle_op_error(e: Exception):
            logger.error(f"{log_prefix}Error judging word {ask.elem[2]}/{ask.elem[3]}: {e}")
            print_error_traceback(e, logger)

        return handle_op_error

    async def save_results(word_tasks: list[asyncio.Task]):
        await asyncio.gather(*word_tasks)
        logger.debug(f"{log_prefix}Judged {sum(judged)} of {len(judged)} words by request")
        if plan.auto or any(judged):
            save_note()
        progress_updater.increment_counts(notes_done=1)

    def spawn_note_tasks(tasks: list[asyncio.Task]) -> None:
        word_tasks: list[asyncio.Task] = []
        for ask_index, ask in enumerate(plan.asks):
            process_word = make_inner_bulk_op(
                config=config,
                op=judge_op,
                gate=gate,
                progress_updater=progress_updater,
                handle_op_error=make_error_handler(ask),
                handle_op_result=lambda _: None,
                cancel_state=cancel_state,
            )
            task = asyncio.create_task(
                process_word(
                    notes_to_add_dict=notes_to_add_dict,
                    notes_to_update_dict=notes_to_update_dict,
                    ask_index=ask_index,
                )
            )
            word_tasks.append(task)
            tasks.append(task)
        tasks.append(asyncio.create_task(save_results(word_tasks)))

    return NotePlan(task_count=len(plan.asks), spawn=spawn_note_tasks)


def make_bulk_op(states: frozenset[MatchState]):
    async def bulk_word_matching_judge_op(
        col: Collection,
        notes: Sequence[Note],
        edited_nids: list[NoteId],
        progress_updater: AsyncTaskProgressUpdater,
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
    ):
        config = mw.addonManager.getConfig(__name__)
        if not config:
            showWarning("Missing addon configuration")
            return
        return await bulk_nested_notes_op(
            message=MODE_NAMES[states],
            config=config,
            bulk_inner_op=partial(plan_word_matching_judge, states=states),
            col=col,
            notes=notes,
            edited_nids=edited_nids,
            progress_updater=progress_updater,
            notes_to_add_dict=notes_to_add_dict,
            notes_to_update_dict=notes_to_update_dict,
            model=judge_model(config),
        )

    return bulk_word_matching_judge_op


def word_matching_judge_from_selected_notes(
    nids: Sequence[NoteId], parent: Browser, states: frozenset[MatchState] = JUDGE_NEW
):
    progress_updater = AsyncTaskProgressUpdater(title=f"Async AI op: {MODE_NAMES[states]}")
    done_text = "Judged words matchability"
    return selected_notes_op(done_text, make_bulk_op(states), nids, parent, progress_updater)
