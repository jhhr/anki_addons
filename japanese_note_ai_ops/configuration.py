import json
import os
import platform
import sys
from enum import Enum
from typing import Callable, Optional, TypedDict, Union
from anki.notes import NoteId

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
ADDON_USER_FILES_DIR = os.path.join(
    ADDON_DIR,
    "user_files",
)

# Ensure the user_files directory exists
os.makedirs(ADDON_USER_FILES_DIR, exist_ok=True)


# Raw word, tuple of 1) word, 2) reading
RawOneMeaningWordType = tuple[str, str]
# Raw word, tuple of 1) word, 2) reading and 3) meaning number
# meaning number is used to indicate the same word and reading occurring with different meanings
RawMultiMeaningWordType = tuple[str, str, int]
# Matched word, same but with note sort field value and note ID
# 1) word, 2) reading, 3) note_sort_field_value, 4) the note's link ID
# note_sort_field_value is different for each meaning a word with the same reading can have
# so it is used to distinguish between them
# The link ID (link_ids.py) names the exact note that the word is matched to: an existing
# note's, or the one generated for a new note the run is making, which it keeps once added
OneMeaningMatchedWordType = tuple[str, str, str, Union[NoteId, int]]
MultiMeaningMatchedWordType = tuple[str, str, int, str, Union[NoteId, int]]

MEANINGS_DICT_FILE = "_all_meanings_dict.json"
KANJI_STORY_COMPONENT_WORDS_LOG = "_kanji_story_component_words.json"


# The json is a dict of "word_reading" to an array of dicts
class GeneratedMeaningType(TypedDict):
    jp_meaning: str
    en_meaning: str


GeneratedMeaningsDictType = dict[str, list[GeneratedMeaningType]]

NO_DICTIONARY_ENTRY_TAG = "2-no-dictionary-entry"
MEANINGS_GENERATED_TAG = "2-meanings-generated-to-json"
MEANING_MAPPED_TAG = "2-note-mapped-to-generated-meaning"
# On a note whose word array is to be generated again. Set by hand, or by split_compound_tag's
# sweep on the notes that a compound newly listed to split has not reached; taken off by
# extract_words once it has generated the note's array.
REGENERATE_TAG = "sentence::regenerate"


class EnAndJPSentence(TypedDict):
    jp_sentence: str
    en_sentence: str


class WordAndSentences(TypedDict):
    jp_meaning: str
    en_meaning: str
    sentences: list[EnAndJPSentence]


class MakeMeaningsResult(Enum):
    SUCCESS = 1
    NO_DICTIONARY_ENTRY = 2
    ERROR = 3


def capture_versions(addon_dir: str = ADDON_DIR) -> dict[str, Optional[str]]:
    """The versions the capture store records on every run: this addon's, Anki's, Python's and
    the platform. Built once, when the store is installed at profile open.

    Each is None when it cannot be read, and nothing here raises: a missing or garbled file must
    not keep the store from recording. The addon's is `human_version` from the manifest.json
    Anki keeps from a released package, else from build.json in a working tree (the package
    ships without build.json, the tree has no manifest).
    """
    return {
        "addon": _addon_version(addon_dir),
        "anki": _read_or_none(_anki_version),
        "python": _read_or_none(platform.python_version),
        "platform": _read_or_none(lambda: sys.platform),
    }


def _addon_version(addon_dir: str) -> Optional[str]:
    for name in ("manifest.json", "build.json"):
        try:
            with open(os.path.join(addon_dir, name), encoding="utf-8") as file:
                version = json.load(file).get("human_version")
        except Exception:
            continue
        if isinstance(version, str) and version:
            return version
    return None


def _anki_version() -> str:
    from anki.buildinfo import version

    return version


def _read_or_none(read: Callable[[], object]) -> Optional[str]:
    try:
        value = read()
    except Exception:
        return None
    return value if isinstance(value, str) else None
