"""The `vocab::split-compound-word` tag: tagging the note about a compound puts the compound on
the config's list of compounds to split (`split_compound_words`).

The list is what `extract_words` hands the generator, and it is a config key, edited in Anki on
a desktop. A compound is met anywhere, on a phone as well, and what is at hand there is the note
about it. So the note is tagged, and this device does the rest once it has synced. For every
note with the tag whose word its list lacks:

- the word goes on the list: the note's own word, and the dictionary form of every array
  element linked to the note, which is what the generator looks a compound up by and may be
  spelled otherwise than the note's field;
- every note whose word array still has that word whole is tagged `sentence:regenerate`. The
  list only reaches an array when the array is made again, and making it again is the user's to
  start: the two sub-words come out unjudged, and judging and matching them costs calls.

Both tag names are hardcoded.

The tag stays on the note. A note whose word the list has is passed over, so a sweep with nothing
new costs a tag search and one read per tagged note. It is not taken off once the word is
listed because the list is not safe where it is: a config is one file per device, and Addon
Config Sync replaces that file whole, with another device's or with the copy it last sent out
(`on_media_sync_did_start_or_stop`). A word that falls off the list that way goes back on at
the next sweep for as long as its note is tagged, and within one session of Anki without a
second tooltip or a second tagging of its notes (`_listed_this_session`). Taking a word off for
good therefore means untagging its note too.

Fixing that copying back in Addon Config Sync would make the sweep after every media sync and
the session's memory unnecessary; the tag staying would still cover another device's config
winning over this one's.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import NamedTuple, Optional, Sequence

from anki.collection import Collection, OpChanges
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp
from aqt.utils import tooltip

from .call_logging import start_call_log
from .word_array import generator
from .word_array.match_flags import (
    iter_words,
    matched_note_id,
    plain_text,
    read_word_array,
    word_array_link_regex,
    word_array_whole_word_regex,
)

logger = logging.getLogger(__name__)

SPLIT_COMPOUND_TAG = "vocab::split-compound-word"
REGENERATE_TAG = "sentence:regenerate"
WORD_FIELD_KEY = "word_kanjified_field"
WORD_LIST_FIELD_KEY = "word_list_field"
UNDO_LABEL = "Tag sentences to regenerate"
SWEEP_DELAY_MS = 100
TOOLTIP_MS = 6000


class Compound(NamedTuple):
    """A note tagged SPLIT_COMPOUND_TAG, with what a sweep needs of it."""

    note_id: int
    word: str  # its word field, without furigana or tags
    notetype: str  # its note type's name
    list_field: str  # that note type's word array field


@dataclass
class Listing:
    """What listing some compounds came to; `changes` is what CollectionOp reads."""

    changes: OpChanges
    forms: dict[str, list[str]]  # per compound's word, what is to go on the list for it
    tagged: int  # the notes REGENERATE_TAG was added to


# The words this session's sweeps have listed, each with the forms listed for it. A word found
# off the list again was taken off by a config copied back over this device's
# (`on_media_sync_did_start_or_stop`), which can happen after every media sync until a
# collection sync has sent the list out. The user was told the first time, and the notes were
# tagged then: a note they have regenerated and untagged since, to find the compound has no two
# words to split into, is not tagged again every few minutes. Later times in the session put
# the word back and nothing else.
_listed_this_session: dict[str, list[str]] = {}


def tagged_compounds(col: Collection, config: dict) -> list[Compound]:
    """The notes tagged SPLIT_COMPOUND_TAG that have a word to list: of a note type the config
    names a word field and a word array field for, with something in the first."""
    compounds = []
    for note_id in col.find_notes(f'"tag:{SPLIT_COMPOUND_TAG}"'):
        note = col.get_note(note_id)
        # The search finds the tag's children too (`...::anything`), which say something else
        if not note.has_tag(SPLIT_COMPOUND_TAG):
            continue
        notetype = note.note_type()
        name = notetype["name"] if notetype else ""
        fields = config.get(name)
        if not isinstance(fields, dict):
            logger.warning("nid:%s is tagged, but its note type has no fields configured", note_id)
            continue
        word_field, list_field = fields.get(WORD_FIELD_KEY), fields.get(WORD_LIST_FIELD_KEY)
        word = plain_text(note[word_field]) if word_field and word_field in note else ""
        if not word or not list_field:
            logger.warning("nid:%s is tagged, but has no word or no word array field", note_id)
            continue
        compounds.append(Compound(note_id, word, name, list_field))
    return compounds


def not_listed(compounds: Sequence[Compound], config: dict) -> list[Compound]:
    listed = generator.split_compounds_of(config)
    return [compound for compound in compounds if compound.word not in listed]


def _array_search(compound: Compound, regex: str) -> str:
    return f'"note:{compound.notetype}" "{compound.list_field}:re:{regex}"'


def compound_forms(col: Collection, compound: Compound) -> list[str]:
    """What to list for a compound: its note's word, then the dictionary form of every array
    element linked to the note, where an array shows the word by another."""
    forms = [compound.word]
    linked_to_it = _array_search(compound, word_array_link_regex(compound.note_id))
    for note_id in col.find_notes(linked_to_it):
        array, _problem = read_word_array(col.get_note(note_id)[compound.list_field])
        for _depth, element in iter_words(array or []):
            if matched_note_id(element) == compound.note_id and element[2] not in forms:
                forms.append(element[2])
    return forms


def list_compounds(col: Collection, compounds: Sequence[Compound]) -> Listing:
    """Tag REGENERATE_TAG every note whose word array has one of the compounds whole, in one
    undo step, and say which forms are to go on the list.

    Writing the list is the caller's, and comes after: notes tagged and no word listed is put
    right by the next sweep, which finds the word still unlisted, while a word listed and its
    notes not tagged would be put right by nothing."""
    forms: dict[str, list[str]] = {}
    searched: set[str] = set()
    note_ids: dict = {}
    for compound in compounds:
        # Two notes can be about one word, a meaning each
        of_the_word = forms.setdefault(compound.word, [])
        for form in compound_forms(col, compound):
            if form not in of_the_word:
                of_the_word.append(form)
            if form in searched:
                continue
            searched.add(form)
            has_it_whole = _array_search(compound, word_array_whole_word_regex(form))
            note_ids.update(dict.fromkeys(col.find_notes(has_it_whole)))
    if not note_ids:
        return Listing(OpChanges(), forms, 0)
    position = col.add_custom_undo_entry(UNDO_LABEL)
    tagged = col.tags.bulk_add(list(note_ids), REGENERATE_TAG).count
    return Listing(col.merge_undo_entries(position), forms, tagged)


def users_config() -> Optional[dict]:
    """The config the user set themselves, without the shipped defaults under it; None when
    there is a meta.json that cannot be read.

    Anki's `addonMeta` answers `{}` for a file that is not JSON as it does for no file, and
    `writeConfig` builds on that answer. Unattended, that would replace a file that is being
    written, or that a person could still repair, and every key in it, with this one list."""
    addon = mw.addonManager.addonFromModule(__name__)
    meta = mw.addonManager.addonMeta(addon)
    if not meta:
        path = os.path.join(mw.addonManager.addonsFolder(addon), "meta.json")
        if os.path.exists(path) and os.path.getsize(path) > len("{}"):
            return None
    return dict(meta.get("config") or {})


def add_to_list(forms: Sequence[str]) -> Optional[list[str]]:
    """Put the forms this device's list lacks on it. Returns those, or None when the config
    could not be written, which leaves the list as it was for the next sweep."""
    config = mw.addonManager.getConfig(__name__) or {}
    value = generator.split_compounds_with(config, forms)
    if value is None:
        return []
    listed = generator.split_compounds_of(config)
    # The user's own config with this one key changed. `getConfig` gives it merged over the
    # shipped defaults, and writing that back would copy every default into the user's file,
    # where a default changed in a later version no longer reaches it.
    user_config = users_config()
    if user_config is None:
        logger.warning("meta.json could not be read; the list is left for the next sweep")
        return None
    user_config[generator.SPLIT_COMPOUNDS_KEY] = value
    mw.addonManager.writeConfig(__name__, user_config)
    return [form for form in dict.fromkeys(forms) if form and form not in listed]


def summary(added: Sequence[str], tagged: int) -> str:
    parts = []
    if added:
        parts.append(f"Compounds to split: {', '.join(added)} listed.")
    if tagged:
        parts.append(f"{tagged} {'note' if tagged == 1 else 'notes'} tagged {REGENERATE_TAG}.")
    return " ".join(parts)


def _on_listed(listing: Listing) -> None:
    added = add_to_list([form for forms in listing.forms.values() for form in forms])
    logger.info("listed %s; %d notes tagged %s", added, listing.tagged, REGENERATE_TAG)
    if added is not None:
        _listed_this_session.update(listing.forms)
    if added or listing.tagged:
        tooltip(summary(added or [], listing.tagged), parent=mw, period=TOOLTIP_MS)


def sweep() -> None:
    """List the words of the tagged notes that are not on the list, and tag their sentences."""
    col = mw.col
    config = mw.addonManager.getConfig(__name__)
    if col is None or not config:
        return
    compounds = not_listed(tagged_compounds(col, config), config)
    again = {
        compound.word: _listed_this_session[compound.word]
        for compound in compounds
        if compound.word in _listed_this_session
    }
    if again:
        add_to_list([form for forms in again.values() for form in forms])
        logger.info("back on the list: %s", list(again))
    compounds = [compound for compound in compounds if compound.word not in again]
    if not compounds:
        return
    # A log file of its own, as a menu op gets one, and only when there is something to do.
    # Opening it detaches the previous file, which is never a run's still being written: the
    # timer a sweep runs from holds off for as long as a progress dialog, so a run, is up.
    start_call_log("split_compound_tag")
    logger.info("tagged and not listed: %s", [compound.word for compound in compounds])
    CollectionOp(parent=mw, op=lambda col: list_compounds(col, compounds)).success(
        _on_listed
    ).run_in_background()


def _sweep_later() -> None:
    # A timer of Anki's: it holds off while a progress dialog is up, so a sweep never starts
    # under a sync's or a bulk run's, and it does not fire once the profile has closed
    mw.progress.single_shot(SWEEP_DELAY_MS, sweep, True)


def on_sync_did_finish() -> None:
    """The collection sync is over, so the tags other devices set are here. The media sync it
    started is mostly still running, and then the sweep is `on_media_sync_did_start_or_stop`'s:
    Addon Config Sync copies every add-on's config out to the media folder when a sync starts
    and back from it when the media sync stops, wherever the two differ, so a list written in
    between is replaced by the one from before the sync. Here only when no media sync follows
    (the sync failed, or left nothing to wait for)."""
    if not mw.media_syncer.is_syncing():
        _sweep_later()


def on_media_sync_did_start_or_stop(running: bool) -> None:
    """A sweep once a media sync has stopped, after this call rather than in it: the copying
    back is another handler of this same hook, and which of the two runs first is the order
    Anki loaded the add-ons in.

    After every media sync, Anki's periodic one included, which no collection sync starts and
    which brings no tags: it is the copying back that takes a word off the list again, when
    the word was listed after the last collection sync began, and this is what puts it back
    right behind it. The next collection sync then sends the list out with the word on it."""
    if not running:
        _sweep_later()


def init_split_compound_tag() -> None:
    gui_hooks.sync_did_finish.append(on_sync_did_finish)
    gui_hooks.media_sync_did_start_or_stop.append(on_media_sync_did_start_or_stop)
