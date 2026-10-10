import re
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional, Union
from anki.notes import Note, NoteId
from anki.collection import Collection
from aqt import mw
from aqt.browser import Browser
from aqt.utils import showWarning
from collections.abc import Sequence

from .chain_types import ChainStep
from .base_ops import (
    get_response,
    bulk_notes_op,
    selected_notes_op,
    AsyncTaskProgressUpdater,
)
from ..utils import get_field_config

logger = logging.getLogger(__name__)


K_WORD_REC = re.compile(r"<k>([^<]*)</k>")
K_INNER_REC = re.compile(r"( [\d々\u4e00-\u9faf\u3400-\u4dbf]+\[[^[]*\][^>[]*?)")

SO_WITHOUT_NO = re.compile(r"<k> 其\[そ\]</k>の?")
SO_INSIDE_NO = re.compile(r" <k> 其\[その\]</k>")

K_INNER_REVERSING_REC = re.compile(r" [\d々\u4e00-\u9faf\u3400-\u4dbf]+\[([^[]*)\]([^>[]*?)")


def make_k_word_replacer(sentence: str):
    # Check if the match can be found in the original sentence
    k_words_in_sentence = False

    def inner_k_word_replacer(match: re.Match[str]) -> str:
        nonlocal k_words_in_sentence
        word = match.group(1)
        if word in sentence:
            # remove <k> tags if the word was kanjified in the original sentence
            k_words_in_sentence = True
            return word
        else:
            # Keep the <k> word unchanged
            return match.group(0)

    def k_word_replacer(match: re.Match[str]) -> str:
        nonlocal k_words_in_sentence
        # the match is of the form <k>...</k> which may contain multiple furigana words
        k_words_in_sentence = False
        res = K_INNER_REC.sub(inner_k_word_replacer, match.group(1))
        if k_words_in_sentence:
            return res
        else:
            return match.group(0)

    return k_word_replacer


def inner_k_word_reversing_replacer(match: re.Match[str]) -> str:
    furigana = match.group(1)
    okurigana = match.group(2) or ""
    return f"{furigana}{okurigana}"


def k_word_reversing_replacer(match: re.Match[str]) -> str:
    # the match is of the form <k>...</k> which may contain multiple furigana words
    return K_INNER_REVERSING_REC.sub(inner_k_word_reversing_replacer, match.group(1))


B_TAGS_REC = re.compile(r"<b>|</b>")

NUMBER_FURI_REC = re.compile(r"(\d+)\[([^\]]+)\]([あ-ん]*)")


def make_number_furi_replacer(sentence: str):
    def number_furi_replacer(match: re.Match[str]) -> str:
        # Check if the original sentence has the number with furigana or not
        number = match.group(1)
        okurigana = match.group(3) or ""
        whole_match = match.group(0)
        if whole_match in sentence:
            # It has furigana too, keep the number with furigana
            return whole_match
        else:
            # it doesn't, so return just the number and okurigana
            return number + okurigana

    return number_furi_replacer


KANJIFIED_SENTENCE_RETURN_FIELD = "kanjified_sentence"
KANJIFY_SENTENCE_DEFAULT_TEMPERATURE = 0.1


POLICY_PATH = Path(__file__).with_name("kanjify_policy.md")


@lru_cache(maxsize=1)
def kanjify_policy() -> str:
    """The kanjification policy, which the prompt holds whole.

    It is the one written statement of what gets kanjified and how: the agents that labelled
    the kanjify golden set (word_array/research) read the same file, and the op is scored
    against their labels. The prompt had a rule list of its own before, older than the policy
    and at odds with it (loanwords, そう, てください, よる), so the op and its eval set disagreed
    by design. The file ships beside this module because the zip leaves research/ out.
    """
    return POLICY_PATH.read_text(encoding="utf-8")


PROMPT_HEAD = """You kanjify one Japanese sentence from a language-learning flash card, following the kanjification policy below. The sentence has furigana in brackets after its kanji words (` 漢字[かな]`). Rewrite in kanji every word written in hiragana or katakana that the policy kanjifies, with the spelling the policy gives, and change nothing else.

## How to work

1. Read the sentence whole and work out what it means: the context decides homophones and senses.
2. Look at every word written in hiragana or katakana, and every し, さ, せ, す, なる / なら / なり / なっ, ない, ある, いる, いう, こと, もの, よう, ところ in it. For each decide: kana (a KANA rule, or no dictionary spelling by SOURCE) or kanji (a KANJI rule), and with which spelling (SPELL, SPLIT). A missed kanjification is as wrong as a wrong one: a content word with a dictionary kanji spelling is kanjified with its main spelling, ordinary words too (林檎, 鞄, 一番, 直ぐ, 所謂, 成る程).
3. The policy was written for agents that have dictionary tools and can ask its author. You have neither. Where it names a check done with a tool (JMdict, Sudachi, `kanjify_lookup.py`), judge from what you know of those dictionaries. Where it says to ask or to hand a word back, leave that word as the input has it.
4. Write the sentence in the field format (the FMT rules). Everything outside your `<k>` spans stays exactly as the input has it: particles, the copula, punctuation, the furigana already there, spaces and HTML tags. Turning every `<k>` span back into the kana it reads must give the input; a program checks this and asks again when it does not.

## Kanjification policy

"""

PROMPT_TAIL = f"""

## Output

Return a JSON string with the following key-value pairs:
 "{KANJIFIED_SENTENCE_RETURN_FIELD}": The whole sentence, kanjified.

"""


def get_kanjify_sentence_prompt(sentence: str) -> str:
    # The sentence comes last, after text that is the same for every sentence: a provider can
    # cache that part, and research scripts cut the sentence off at this marker
    return f"{PROMPT_HEAD}{kanjify_policy()}{PROMPT_TAIL}The sentence to process: {sentence}\n"


def kanjify_temperature(config: dict[str, str]) -> float:
    config_temp = config.get("kanjify_sentence_temperature", None)
    if config_temp is None:
        return KANJIFY_SENTENCE_DEFAULT_TEMPERATURE
    try:
        return float(config_temp)
    except ValueError:
        logger.error(
            "Invalid temperature value in config: %s. Using default temperature: %f",
            config_temp,
            KANJIFY_SENTENCE_DEFAULT_TEMPERATURE,
        )
        return KANJIFY_SENTENCE_DEFAULT_TEMPERATURE


def clean_kanjified(sentence: str, kanjified_sentence: str) -> tuple[str, bool]:
    """The model's kanjified sentence with its common mistakes cleaned up, and whether turning
    its <k> spans back into kana gives the original sentence (whitespace and <b> aside)."""
    # Sometimes kanjifying する results in 為[し]る
    kanjified_sentence = kanjified_sentence.replace("<k> 為[し]る</k>", "<k> 為[す]る</k>")
    # Sometimes when kanjifying その it leaves out the の --> <k> 其[そ]</k>
    kanjified_sentence = SO_WITHOUT_NO.sub("<k> 其[そ]の</k>", kanjified_sentence)
    # Or puts the の inside the furigana tags -->  <k> 其[その]</k>
    kanjified_sentence = SO_INSIDE_NO.sub(" <k> 其[そ]の</k>", kanjified_sentence)
    # Sometimes it wraps the sentence in 「」when the original sentence wasn't
    if not (sentence.startswith("「") and sentence.endswith("」")) and (
        kanjified_sentence.startswith("「") and kanjified_sentence.endswith("」")
    ):
        # Remove the wrapping
        kanjified_sentence = kanjified_sentence[1:-1]

    # It may unnecessarily wrap words in <k> tags that were already kanjified in the
    # original sentence
    k_word_replacer = make_k_word_replacer(sentence)
    kanjified_sentence = K_WORD_REC.sub(k_word_replacer, kanjified_sentence)

    # Clean double spaces
    kanjified_sentence = kanjified_sentence.replace("  ", " ")
    # Clean extra space before <k> tags
    kanjified_sentence = kanjified_sentence.replace(" <k> ", "<k> ")

    reversed_sentence = B_TAGS_REC.sub("", kanjified_sentence)
    reversed_sentence = K_WORD_REC.sub(k_word_reversing_replacer, reversed_sentence)
    number_furi_replacer = make_number_furi_replacer(sentence)
    reversed_sentence = NUMBER_FURI_REC.sub(number_furi_replacer, reversed_sentence)
    # Remove all whitespace as the comparison often fails due to trivial differences
    reversed_sentence = re.sub(r"\s", "", reversed_sentence)
    cleaned_sentence = re.sub(r"\s", "", B_TAGS_REC.sub("", sentence))
    if cleaned_sentence != reversed_sentence:
        logger.debug("Reversed %s != original %s", reversed_sentence, cleaned_sentence)
    return kanjified_sentence, cleaned_sentence == reversed_sentence


def get_kanjified_sentence_from_model(
    config: dict[str, str],
    sentence: str,
) -> Union[list[str], None]:
    prompt = get_kanjify_sentence_prompt(sentence)
    model = config.get("kanjify_sentence_model", "")
    result = get_response(
        model,
        prompt,
        temperature=kanjify_temperature(config),
        kind="kanjify.sentence",
        inputs={"sentence": sentence},
    )
    if result is None:
        logger.error("Failed to get a response from the API.")
        # If the prompt failed, return nothing
        return None
    try:
        return [result[KANJIFIED_SENTENCE_RETURN_FIELD]]
    except KeyError:
        logger.error(f"Key '{KANJIFIED_SENTENCE_RETURN_FIELD}' not found in the result.")
        return None


MAX_ATTEMPTS = 5


def kanjify_sentence_in_note(
    config: dict[str, str],
    note: Note,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    attempt: int = 1,
) -> bool:
    model = note.note_type()
    if not model:
        logger.error("Missing note type for note %s", note.id)
        return False
    try:
        furigana_sentence_field = get_field_config(config, "furigana_sentence_field", model)
        kanjified_sentence_field = get_field_config(config, "kanjified_sentence_field", model)
    except Exception as e:
        logger.error("Error getting field config: %s", e)
        return False

    logger.debug("furigana_sentence_field in note: %s", furigana_sentence_field in note)
    logger.debug("kanjified_sentence_field in note: %s", kanjified_sentence_field in note)
    # Check if the note has the required fields
    if furigana_sentence_field in note and kanjified_sentence_field in note:
        # Get the values from fields
        sentence = note[furigana_sentence_field]
        logger.debug("sentence: %s", sentence)
        # Check if the value is non-empty
        if sentence:
            # Clean any <b> tags from the sentence
            # sentence = sentence.replace("<b>", "").replace("</b>", "")
            logger.debug("cleaned sentence: %s", sentence)
            # Call API to get translation
            result = get_kanjified_sentence_from_model(config, sentence)
            logger.debug("result from API: %s", result)
            if result is not None:
                [kanjified_sentence] = result
                logger.debug("kanjified_sentence: %s", kanjified_sentence)
                kanjified_sentence, reverses = clean_kanjified(sentence, kanjified_sentence)

                # Update the note with the new values
                note[kanjified_sentence_field] = kanjified_sentence

                # Tag the note if reversing the kanjification doesn't give the original sentence
                if not reverses:
                    note.add_tag("kanjify_sentence_mismatch")
                    # try again until MAX_ATTEMPTS is reached
                    logger.debug(f"Reversed sentence does not match original:\n{sentence}")
                    if attempt < MAX_ATTEMPTS:
                        logger.debug(
                            "Attempting re-kanjify %d of %d",
                            attempt,
                            MAX_ATTEMPTS,
                        )
                        return kanjify_sentence_in_note(
                            config, note, notes_to_add_dict, notes_to_update_dict, attempt + 1
                        )
                elif note.has_tag("kanjify_sentence_mismatch"):
                    note.remove_tag("kanjify_sentence_mismatch")
                if note.id != 0 and note.id not in notes_to_update_dict:
                    notes_to_update_dict[note.id] = note
                return True
            return False
        return False
    else:
        logger.error("note is missing fields")
    return False


def bulk_kanjify_notes_op(
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
        return
    message = "Kanjifying sentences"
    op = kanjify_sentence_in_note
    return bulk_notes_op(
        message,
        config,
        op,
        col,
        notes,
        edited_nids,
        progress_updater,
        notes_to_add_dict,
        notes_to_update_dict,
    )


def kanjify_selected_notes(
    nids: Sequence[NoteId], parent: Browser, chain: Optional[ChainStep] = None
):
    progress_updater = AsyncTaskProgressUpdater(title="Async AI op: Kanjifying sentences")
    done_text = "Updated kanjified sentences"
    bulk_op = bulk_kanjify_notes_op
    return selected_notes_op(done_text, bulk_op, nids, parent, progress_updater, chain=chain)
