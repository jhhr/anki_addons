"""Phase 2: replace a note's old extract_words word list with a generated word array.

The array is generated locally from the note's own sentence (Sudachi and JMdict, no API call),
and the old list's note ids are fitted into it by `word_array.migrate`. The array replaces the
old list in `word_list_field`: the user's call, since keeping both would mean a second field on
every note type and the old lists are reproducible by re-running extract_words.

The sentence is taken the way extract_words takes it, with the surrounding context sentences
stripped (`strip_context_sentences`): their words are not what the note is about, and giving
them array elements would offer them to match_words_to_notes. The array therefore partitions
the sentence without its `<i>` context rather than the whole field.

A note whose old list held a link the array had no home for is tagged, so that what the
migration dropped can be looked at in the browser afterwards. Entries carrying no note id are
dropped silently - the array is the correct word list now, and that is all such an entry was.

Names Sudachi doesn't know come out as proper nouns when the name lexicon has them
(`build_name_lexicon.py`, saved in user_files); without one they stay cut up.

The first run downloads the Sudachi dictionary and JMdict (~83 MB), which is asked about once
before any note is touched rather than in the middle of a bulk run.
"""

import json
import logging
from functools import partial
from typing import Any, Callable, Optional, Sequence, cast

from anki.collection import Collection
from anki.notes import Note, NoteId
from aqt import mw
from aqt.operations import QueryOp
from aqt.utils import askUser, showWarning

from ..async_api_ops.base_ops import (
    AsyncTaskProgressUpdater,
    bulk_notes_op,
    selected_notes_op,
)
from ..async_api_ops.match_words_to_notes import decode_word_list_field
from ..html_stripping import strip_context_sentences
from ..utils import get_field_config, print_error_traceback
from ..word_array import generator, migrate, names, resources

logger = logging.getLogger(__name__)

LEFTOVERS_TAG = "word-array-migration-leftovers"
MIGRATED_TAG = "word-array-migrated"


def migrate_word_array_in_note(
    config: dict,
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    name_lexicon: Optional[dict] = None,
) -> bool:
    """Generate the word array for one note's sentence, carry its old note ids into it and
    write it over the old word list. False when the note has nothing to migrate."""
    note_type = note.note_type()
    if not note_type:
        logger.error(f"Missing note type for note {note.id}")
        return False
    try:
        sentence_field = get_field_config(config, "word_extraction_sentence_field", note_type)
        word_list_field = get_field_config(config, "word_list_field", note_type)
    except Exception as e:
        logger.error(str(e))
        return False
    if sentence_field not in note or word_list_field not in note:
        return False

    log_prefix = f"Migrate word array--nid:{note.id}--"
    # The same sentence extract_words worked from, so that the array covers the words the old
    # list was made of and no others. The array therefore reconstructs the sentence without its
    # <i> context, not the whole field.
    sentence = strip_context_sentences(note[sentence_field])
    if not sentence:
        logger.debug(f"{log_prefix}No sentence to generate an array from")
        return False

    try:
        arr = generator.generate(sentence, name_lexicon)
    except Exception as e:
        logger.error(f"{log_prefix}Could not generate a word array: {e}")
        print_error_traceback(e, logger)
        return False

    # An empty field is a note extract_words never ran on: the array is all it gets, and there
    # is nothing to carry over or to report.
    word_lists: Any = {}
    if note[word_list_field]:
        word_lists = decode_word_list_field(
            note, word_list_field, cast(dict, notes_to_update_dict), log_prefix
        )
        if word_lists is None:
            # Unreadable even after repair_json, and decode_word_list_field has tagged the note.
            # Overwriting it would throw away links nobody has looked at yet.
            logger.error(f"{log_prefix}Left alone: the old word list could not be read")
            return False

    report = migrate.migrate(word_lists, arr)
    logger.info(f"{log_prefix}{report.summary()}")
    for leftover in report.leftovers:
        logger.info(f"{log_prefix}lost {leftover}")

    note[word_list_field] = json.dumps(arr, ensure_ascii=False)
    note.add_tag(MIGRATED_TAG)
    if report.lost_note_ids:
        note.add_tag(LEFTOVERS_TAG)
    if note.id > 0:
        notes_to_update_dict[note.id] = note
    return True


async def bulk_migrate_word_arrays_op(
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]] = {},
    notes_to_update_dict: dict[NoteId, Note] = {},
):
    config = mw.addonManager.getConfig(__name__)
    if not config:
        showWarning("Missing addon configuration")
        return 0, notes_to_add_dict, notes_to_update_dict, edited_nids
    return await bulk_notes_op(
        message="Migrating word lists to word arrays",
        config=config,
        op=partial(
            migrate_word_array_in_note, name_lexicon=names.load_lexicon(resources.NAME_LEXICON)
        ),
        col=col,
        notes=notes,
        edited_nids=edited_nids,
        progress_updater=progress_updater,
        notes_to_add_dict=notes_to_add_dict,
        notes_to_update_dict=notes_to_update_dict,
        is_sync_op=True,
    )


def _run(nids: Sequence[NoteId], parent: Any):
    progress_updater = AsyncTaskProgressUpdater(title="Sync op: Migrating word lists to arrays")
    done_text = "Migrated word lists to word arrays"
    return selected_notes_op(done_text, bulk_migrate_word_arrays_op, nids, parent, progress_updater)


def with_generator_resources(parent: Any, then: Callable[[], Any]) -> None:
    """Run `then` once the generator can run: SudachiPy present, and the dictionaries it needs
    downloaded, asking the user before the download."""
    if not resources.has_sudachipy():
        showWarning(
            "The word array generator needs SudachiPy, which is missing from this add-on's"
            " libraries. Reinstall the add-on to get it."
        )
        return
    needed = resources.missing()
    if not needed:
        then()
        return

    downloads = "\n".join(f"  - {d.name}, {d.size_mb:.0f} MB" for d in needed)
    if not askUser(
        "The word array generator needs these, once:\n\n"
        f"{downloads}\n\n"
        "They are downloaded into the add-on's user_files, which Anki keeps across add-on"
        " updates. Download them now?",
        parent=parent,
        title="Word array resources",
    ):
        return

    def fetch(_col: Collection) -> None:
        resources.ensure(lambda step: logger.info(f"Word array resources: {step}"))

    QueryOp(
        parent=parent,
        op=fetch,
        success=lambda _: then(),
    ).with_progress("Downloading the word array generator's dictionaries").run_in_background()


def migrate_word_arrays_from_selected(nids: Sequence[NoteId], parent: Any):
    """Replace the selected notes' word lists with generated word arrays, asking first - the
    old list is overwritten, and the ops that read that field have not been taught the new
    format yet - and then for the downloads the generator needs on its first use."""
    lexicon_size = len(names.load_lexicon(resources.NAME_LEXICON))
    lexicon_text = (
        f"The name lexicon has {lexicon_size} names."
        if lexicon_size
        else "There is no name lexicon yet (Build name lexicon from selected notes), so names"
        " Sudachi doesn't know stay cut up."
    )
    if not askUser(
        f"Replace the word list of {len(nids)} note(s) with a generated word array?\n\n"
        "The old list is overwritten in place. Only the note ids of words already matched are"
        " carried over; a note that loses one is tagged"
        f" {LEFTOVERS_TAG}, and every note migrated is tagged {MIGRATED_TAG}.\n\n"
        f"{lexicon_text}\n\n"
        "match_words_to_notes and clean_meaning still read the old format from this field, so"
        " migrated notes will not work with them until they are updated.",
        parent=parent,
        title="Migrate word lists to word arrays",
        defaultno=True,
    ):
        return
    with_generator_resources(parent, lambda: _run(nids, parent))
