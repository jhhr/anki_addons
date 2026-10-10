"""Every op that runs over notes through `selected_notes_op`, in the "AI helper" menu's order.

The browser's context menu and the multi-op dialog both offer these, so the list lives in one
place: a label or an order changed here changes both, and a new op added here shows up in
both. Each `start` begins one run of its op and, given a `ChainStep`, runs it as a step of a
chain, which hears back through `chain.on_done` (see `async_api_ops/chain_types.py`).

A variant of one entry function - a judge mode, a single-word rematch mode - is an op of its
own here, as it is in the menu. The starts look their entry function up when called rather
than binding it when this module loads, so a test can patch the function where it is defined
here.

Two menu entries are not here: building the name lexicon and exporting kanjify test data do
not run through `selected_notes_op` and write no notes, so they are no step of a chain. The
menu adds them itself (`ai_helper_menu.py`).

This imports every op module, and through them the vendored packages, so like them it may be
imported only inside `__init__.py`'s guarded `try/except ImportError` block.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable, Optional

from anki.notes import NoteId

from .async_api_ops.chain_types import ChainStep
from .async_api_ops.clean_meaning import clean_selected_notes
from .async_api_ops.extract_words import (
    extract_words_and_judge_from_selected_notes,
    extract_words_from_selected_notes,
    regenerate_words_from_selected_notes,
)
from .async_api_ops.find_proper_nouns import find_proper_nouns_from_selected_notes
from .async_api_ops.kanjify_sentence import kanjify_selected_notes
from .async_api_ops.make_all_meanings import (
    make_meanings_selected_notes,
    merge_meanings_selected_notes,
)
from .async_api_ops.make_kanji_story import make_stories_for_selected_notes
from .async_api_ops.match_words_to_notes import (
    match_single_word_to_notes_from_selected,
    match_words_to_notes_from_selected,
)
from .async_api_ops.new_note_all_ops import new_note_all_ops_selected_notes
from .async_api_ops.translate_field import translate_selected_notes
from .async_api_ops.word_matching_judge import word_matching_judge_from_selected_notes
from .sync_local_ops.deduplicate_existing_meaning_notes import (
    deduplicate_existing_meaning_notes_selected_notes,
)
from .sync_local_ops.find_missing_matched_note_ids import (
    find_missing_matched_note_ids_selected_notes,
)
from .sync_local_ops.tag_notes_matched_status import tag_notes_matched_status_from_selected
from .word_array.match_flags import JUDGE_NEW, REJUDGE_ALL, REJUDGE_MATCHED

# The menu puts a separator between the two
GROUP_ASYNC = "async"
GROUP_SYNC = "sync"

StartOp = Callable[[Sequence[NoteId], Any, Optional[ChainStep]], None]


@dataclass(frozen=True)
class OpSpec:
    # Stable and snake_case: a saved chain or a config value may name an op by it one day,
    # which a label reworded for the menu would break
    key: str
    # The menu entry's text, word for word
    label: str
    # start(nids, parent, chain): begins the run and returns; the run itself ends later
    start: StartOp
    # Wrapped in `with_generator_resources`: a chain asks about the downloads once, up front
    needs_generator: bool
    group: str


OPS: tuple[OpSpec, ...] = (
    OpSpec(
        "clean_meaning",
        "Clean dictionary meaning",
        lambda nids, parent, chain: clean_selected_notes(nids, parent=parent, chain=chain),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "translate_sentence",
        "Translate sentence",
        lambda nids, parent, chain: translate_selected_notes(nids, parent=parent, chain=chain),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "kanji_story",
        "Generate kanji story",
        lambda nids, parent, chain: make_stories_for_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "kanjify_sentence",
        "Kanjify sentence",
        lambda nids, parent, chain: kanjify_selected_notes(nids, parent=parent, chain=chain),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "extract_words",
        "Extract words",
        lambda nids, parent, chain: extract_words_from_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=True,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "extract_words_and_judge",
        "Extract words + Judge matchability",
        lambda nids, parent, chain: extract_words_and_judge_from_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=True,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "regenerate_words",
        "Regenerate words over the current array",
        lambda nids, parent, chain: regenerate_words_from_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=True,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "find_proper_nouns",
        "Find proper nouns in word arrays",
        lambda nids, parent, chain: find_proper_nouns_from_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=True,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "judge_words",
        "Judge words matchability",
        lambda nids, parent, chain: word_matching_judge_from_selected_notes(
            nids, parent=parent, states=JUDGE_NEW, chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "rejudge_matched_words",
        "Re-judge matched words",
        lambda nids, parent, chain: word_matching_judge_from_selected_notes(
            nids, parent=parent, states=REJUDGE_MATCHED, chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "rejudge_all_words",
        "Re-judge matched/judged words",
        lambda nids, parent, chain: word_matching_judge_from_selected_notes(
            nids, parent=parent, states=REJUDGE_ALL, chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "match_words",
        "Match extracted words to notes",
        lambda nids, parent, chain: match_words_to_notes_from_selected(
            nids, parent=parent, chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "rematch_single_word",
        "Rematch all single word to notes",
        lambda nids, parent, chain: match_single_word_to_notes_from_selected(
            nids, parent=parent, reprocess_words="both", chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "rematch_processed_single_word",
        "Rematch processed single words to notes",
        lambda nids, parent, chain: match_single_word_to_notes_from_selected(
            nids, parent=parent, reprocess_words="only_processed", chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "match_remaining_single_word",
        "Match remaining unprocessed single words to notes",
        lambda nids, parent, chain: match_single_word_to_notes_from_selected(
            nids, parent=parent, reprocess_words="only_unprocessed", chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "make_all_meanings",
        "Generate all meanings for selected notes",
        lambda nids, parent, chain: make_meanings_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "merge_meanings",
        "Merge existing meanings for selected notes",
        lambda nids, parent, chain: merge_meanings_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=False,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "new_note_all_ops",
        "Run all ops for new notes",
        lambda nids, parent, chain: new_note_all_ops_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=True,
        group=GROUP_ASYNC,
    ),
    OpSpec(
        "find_missing_matched_note_ids",
        "Find missing matched note ids for selected notes",
        lambda nids, parent, chain: find_missing_matched_note_ids_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=False,
        group=GROUP_SYNC,
    ),
    OpSpec(
        "tag_notes_matched_status",
        "Tag notes matched status",
        lambda nids, parent, chain: tag_notes_matched_status_from_selected(
            nids, parent=parent, chain=chain
        ),
        needs_generator=False,
        group=GROUP_SYNC,
    ),
    OpSpec(
        "deduplicate_existing_meaning_notes",
        "Deduplicate existing meaning notes",
        lambda nids, parent, chain: deduplicate_existing_meaning_notes_selected_notes(
            nids, parent=parent, chain=chain
        ),
        needs_generator=False,
        group=GROUP_SYNC,
    ),
)

OP_BY_KEY: dict[str, OpSpec] = {spec.key: spec for spec in OPS}
