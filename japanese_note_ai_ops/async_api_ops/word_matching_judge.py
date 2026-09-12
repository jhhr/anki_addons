"""Phase 3: the word matching judge, which decides which words of a word array get a note.

The generator's array errs towards more words, so study value is decided here, in `match_data`
(see `word_array.match_flags`): the judge is shown the sentence once per word, the word marked in
`<b>`, picks the numbered ones not worth a note, and every other numbered word is judged `match`. Which words are numbered
is the mode, a set of `MatchState`s:

- `JUDGE_NEW` - only unjudged words, `[]`. The default; nothing already decided is touched.
- `REJUDGE_MATCHED` - words with a note id, `[id]` and `[id, quality]`. A pick unlinks the word.
- `REJUDGE_ALL` - every judged word, states 2-5.

A note with no word in the mode's states makes no request. A note whose field still holds an old
extract_words word list is skipped: the judge only reads arrays (migrate_word_arrays makes them).
Note ids a re-judge unlinked are logged, so a link lost to a bad call can be put back.
"""

import json
import logging
from collections.abc import Sequence
from functools import partial
from typing import Callable, Iterable, Optional, Union

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning

from .base_ops import (
    get_response,
    bulk_notes_op,
    selected_notes_op,
    AsyncTaskProgressUpdater,
)
from ..utils import get_field_config
from ..word_array import match_flags
from ..word_array.match_flags import (
    JUDGE_NEW,
    REJUDGE_ALL,
    REJUDGE_MATCHED,
    MatchState,
    decode_word_array,
)

logger = logging.getLogger(__name__)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        match_flags.JUDGE_RETURN_FIELD: {"type": "array", "items": {"type": "integer"}},
    },
    "required": [match_flags.JUDGE_RETURN_FIELD],
    "additionalProperties": False,
}

MODE_NAMES = {
    JUDGE_NEW: "Judging words matchability",
    REJUDGE_MATCHED: "Re-judging matched words",
    REJUDGE_ALL: "Re-judging matched/judged words",
}


def judge_model(config: dict) -> str:
    return config.get("word_matching_judge_model") or config.get("extract_words_model", "")


def judge_word_array(
    arr: list,
    states: Iterable[MatchState],
    ask: Callable[[str], Union[dict, None]],
    log_prefix: str = "",
) -> Optional[list[int]]:
    """Judge the words of `arr` in `states` in place, asking the model through `ask`. Returns the
    note ids the judge unlinked, or None when nothing was judged: no word to judge, no response,
    or a response without the picks."""
    prompt, elements = match_flags.judge_prompt(arr, states)
    if not elements:
        logger.debug(f"{log_prefix}No words to judge")
        return None
    response = ask(prompt)
    if response is None:
        logger.error(f"{log_prefix}No response from the judge")
        return None
    try:
        return match_flags.apply_judge_response(elements, response)
    except ValueError as e:
        logger.error(f"{log_prefix}{e}")
        return None


def word_matching_judge_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    states: Iterable[MatchState] = JUDGE_NEW,
) -> bool:
    note_type = note.note_type()
    if not note_type:
        logger.error(f"Missing note type for note {note.id}")
        return False
    try:
        word_list_field = get_field_config(config, "word_list_field", note_type)
    except Exception as e:
        logger.error(str(e))
        return False
    if word_list_field not in note:
        return False

    log_prefix = f"Word matching judge--nid:{note.id}--"
    # The array's raw texts make up the sentence, so the prompt is built from the array alone
    arr = decode_word_array(note[word_list_field])
    if arr is None:
        logger.debug(f"{log_prefix}The word list field holds no word array")
        return False

    model = judge_model(config)
    try:
        unlinked = judge_word_array(
            arr,
            states,
            lambda prompt: get_response(model, prompt, response_schema=RESPONSE_SCHEMA),
            log_prefix,
        )
    except ValueError as e:
        # match_state on match_data it doesn't know: leave the note for a look
        logger.error(f"{log_prefix}{e}")
        return False
    if unlinked is None:
        return False
    for note_id in unlinked:
        logger.warning(f"{log_prefix}unlinked note {note_id}")

    note[word_list_field] = json.dumps(arr, ensure_ascii=False)
    if note.id > 0:
        notes_to_update_dict[note.id] = note
    return True


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
        return await bulk_notes_op(
            MODE_NAMES[states],
            config,
            partial(word_matching_judge_in_note, states=states),
            col,
            notes,
            edited_nids,
            progress_updater,
            notes_to_add_dict,
            notes_to_update_dict,
        )

    return bulk_word_matching_judge_op


def word_matching_judge_from_selected_notes(
    nids: Sequence[NoteId], parent: Browser, states: frozenset[MatchState] = JUDGE_NEW
):
    progress_updater = AsyncTaskProgressUpdater(title=f"Async AI op: {MODE_NAMES[states]}")
    done_text = "Judged words matchability"
    return selected_notes_op(done_text, make_bulk_op(states), nids, parent, progress_updater)
