"""Find missing matched note ids (sync_local_ops/find_missing_matched_note_ids.py) in a real
collection, run through a NotesRunSpec as a script runs it.

A word linked to an ID no note holds goes back to `["match"]`; a legacy negative placeholder is
left for the match op. A link is a link ID (link_ids.py): the ID a note's link ID field holds,
else its note id. So a note holding a generated ID is found by it, and its note id is no link
to it; a `nid:` search answers both the other way round.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest
from anki.notes import NoteId

from anki_shared.testing import real_anki
from japanese_note_ai_ops.async_api_ops import base_ops
from japanese_note_ai_ops.sync_local_ops.find_missing_matched_note_ids import (
    bulk_find_missing_matched_note_ids_op,
)
from japanese_note_ai_ops.word_array.match_flags import format_word_array, read_word_array

PACKAGE = "japanese_note_ai_ops"
VOCAB = "Japanese vocab note"
SENTENCE = "Sentence note"
# A note type no block of the config names, whose notes the match op can still have linked
OTHER = "Other note"

ONE_TYPE = {
    VOCAB: {"word_list_field": "Words", "sentence_field": "Word", "word_sort_field": "Word sort"},
    "log_to_console": False,
}
TWO_TYPE_IDS = {
    SENTENCE: {
        "vocab_note_type": VOCAB,
        "word_list_field": "Words",
        "sentence_field": "Sentence",
        "sentence_id_field": "Sentence ID",
    },
    VOCAB: {
        "sentence_note_type": SENTENCE,
        "word_sort_field": "Word sort",
        "new_note_id_field": "Word ID",
    },
    "log_to_console": False,
}

WORD_LINK_ID = 1160697660039337
UNHELD_LINK_ID = 1841288010442579
PLACEHOLDER = -1234567


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Any]:
    stub = real_anki.install()
    saved = (stub.col, dict(stub.addonManager.configs))
    collection = real_anki.open_collection(tmp_path / "collection.anki2")
    try:
        real_anki.make_note_type(collection, VOCAB, ["Word sort", "Word", "Words", "Word ID"])
        real_anki.make_note_type(collection, SENTENCE, ["Sentence", "Words", "Sentence ID"])
        real_anki.make_note_type(collection, OTHER, ["Front"])
        stub.col = collection
        stub.progress.set_cancel(False)
        yield collection
    finally:
        stub.progress.set_cancel(False)
        stub.col, stub.addonManager.configs = saved
        collection.close()


def set_config(config: dict) -> None:
    real_anki.install().addonManager.configs[PACKAGE] = json.loads(json.dumps(config))


def word(form: str, match_data: list) -> list:
    return [form, "noun", form, form, match_data, []]


def vocab(col: Any, form: str, word_id: str = "", words: Optional[list] = None) -> Any:
    fields = {"Word sort": form, "Word": form, "Word ID": word_id}
    if words:
        fields["Words"] = format_word_array(words)
    return real_anki.add_note(col, VOCAB, fields)


def find_missing(col: Any, nids: list[int]) -> Any:
    spec = base_ops.NotesRunSpec(
        "Updated missing matched note ids",
        "Sync op: Finding missing matched note ids",
        bulk_find_missing_matched_note_ids_op,
    )
    run, result = spec.notes_run([NoteId(nid) for nid in nids])
    run(col)
    return result


def match_data(col: Any, nid: int) -> list:
    array, problem = read_word_array(col.get_note(nid)["Words"])
    assert problem is None and array is not None
    return [element[4] for element in array]


def test_links_by_note_id_stay_while_their_notes_exist(col):
    # The data as it is: no link ID field, so every note's link ID is its note id
    set_config(ONE_TYPE)
    cat = vocab(col, "猫")
    gone = vocab(col, "犬")
    other = real_anki.add_note(col, OTHER, {"Front": "鳥"})
    col.remove_notes([gone.id])
    processed = vocab(
        col,
        "猫と犬と鳥",
        words=[
            word("猫", [cat.id, 4]),
            word("犬", [gone.id]),
            word("鳥", [other.id]),
            word("魚", [PLACEHOLDER]),
        ],
    )

    result = find_missing(col, [processed.id])

    assert result.edited_nids == [processed.id]
    assert match_data(col, processed.id) == [
        [cat.id, 4],
        ["match"],
        [other.id],
        [PLACEHOLDER],
    ]


def test_a_link_is_kept_while_a_note_holds_its_link_id(col):
    set_config(TWO_TYPE_IDS)
    holder = vocab(col, "猫", word_id=str(WORD_LINK_ID))
    plain = vocab(col, "犬")
    gone = vocab(col, "馬")
    other = real_anki.add_note(col, OTHER, {"Front": "鳥"})
    col.remove_notes([gone.id])
    words = [
        word("猫", [WORD_LINK_ID, 4]),
        # The note id of a note holding an ID: no note holds it as its link ID
        word("猫", [holder.id]),
        word("犬", [plain.id]),
        word("馬", [gone.id]),
        word("鳥", [other.id]),
        word("魚", [PLACEHOLDER]),
        word("牛", [UNHELD_LINK_ID]),
    ]
    processed = real_anki.add_note(
        col, SENTENCE, {"Sentence": "文", "Words": format_word_array(words)}
    )

    result = find_missing(col, [processed.id])

    assert result.edited_nids == [processed.id]
    assert match_data(col, processed.id) == [
        [WORD_LINK_ID, 4],
        ["match"],
        [plain.id],
        ["match"],
        [other.id],
        [PLACEHOLDER],
        ["match"],
    ]
