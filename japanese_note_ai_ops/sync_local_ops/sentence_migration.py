"""Planning "Move sentences to sentence notes": the sentence notes to make from the vocab notes
that hold the sentences today, the existing ones to join, and the example each vocab note gets.

The op reads every vocab note of the type, the sentence notes already there and the selection,
and hands them here as plain records; everything it writes follows from the plan this returns.
Its pre-flight is here too (`preflight_error`), given the collection's note types and decks as
plain data.
The rules are where a mistake costs: a wrong link puts the wrong word in bold on a card,
silently and for good, while a case the plan cannot decide is listed in the report and left to
a person. So wherever a rule cannot decide, the plan links nothing and reports it.

Records are keyed by config key (`note_roles.SENTENCE_KEYS`, `word_list_field`), never by field
name: by the time the migration runs the vocab block names no sentence fields, and the op reads
the vocab notes' old ones by the sentence block's names. Free of anki and aqt, so all of it is
tested on hand-written arrays.

Terms, as in the plan of this work: a *source* is a vocab note without `new_matched_jp_word`,
one the user added, and the sentences that move are the sources'. A note with the tag is one the
match op made, holding a copy of the sentence it was found in, and the migration leaves it
alone: it gets no example and loses no tag, and a sentence only such notes hold gets no sentence
note. Its link ID is still read, since the sources' arrays link it by that. A *sentence key* is
a sentence's plain text (`match_flags.plain_text` after `strip_context_sentences`); an array's
is the plain text of its top-level raw texts, which is the key of the field it was generated
from.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from ..html_stripping import strip_context_sentences
from ..kana_conv import to_hiragana
from ..link_ids import LinkIndex, link_id, parse_link_id, placeholder_holders
from ..note_roles import SENTENCE_KEYS, is_vocab_type, layout_error, sentence_type_of
from ..word_array import match_flags, match_targets, merge
from ..word_array.match_flags import MatchState

# The tags this concerns. The first three are the addon's own, spelled as the ops that set them
# spell them (each hardcodes its own)
MATCH_OP_TAG = "new_matched_jp_word"
INVALID_ARRAY_TAG = "invalid_word_list_json"
KANJIFY_MISMATCH_TAG = "kanjify_sentence_mismatch"
NEEDS_EXTRACT_TAG = "sentence-needs-extract"
CONFLICT_TAG = "sentence-migration-conflict"
# The addon's tags that are about the sentence: they move from the vocab note a sentence note is
# made from to that sentence note
MOVED_TAGS = (KANJIFY_MISMATCH_TAG, INVALID_ARRAY_TAG)

WORD_LIST_KEY = "word_list_field"
SEEN_COUNT_KEY = "sentence_seen_count_field"
EXAMPLE_ID_KEY = "example_sentence_id_field"
# The link ID fields (link_ids.py, note_roles.link_id_field), which the migration requires: it
# writes a link ID into every note it makes or changes
VOCAB_ID_KEY = "new_note_id_field"
SENTENCE_ID_KEY = "sentence_id_field"
# The vocab block's keys naming the word the link check compares elements with
WORD_KEYS = ("word_kanjified_field", "word_normal_field", "word_reading_field")
# The sentence block's lists of the user's tags (D13), read by the migration only
MOVE_TAGS_KEY = "migration_move_tags"
COPY_TAGS_KEY = "migration_copy_tags"
_EXTRACTION_KEY = "word_extraction_sentence_field"
_FURIGANA_KEY = "furigana_sentence_field"
_AUDIO_KEY = "sentence_audio_field"

# Any <b> or </b>, attributes and case aside; <br> and <blockquote> are not
_BOLD_TAG_RE = re.compile(r"</?b(?:\s[^>]*)?>", re.IGNORECASE)
_OPEN_BOLD_RE = re.compile(r"<b(?:\s[^>]*)?>", re.IGNORECASE)
_CLOSE_BOLD_RE = re.compile(r"</b\s*>", re.IGNORECASE)
# Stand-ins for <b> and </b> while the rest of the html goes: private use characters, which no
# sentence holds (one that does gets no span rather than a wrong one)
_OPEN, _CLOSE = "\ue000", "\ue001"
# A furigana group with no space before it covers only its trailing kanji run
# ("うに開豁[かいかつ]"); the same pattern as word_array/text_map.py's KANJI_TAIL_RE
_KANJI_TAIL_RE = re.compile(r"[\d々ヶヵ一-龯㐀-䶿]+$")


# --- inputs ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class VocabRecord:
    """A vocab note as the planner needs it. `fields` holds the old sentence fields by
    SENTENCE_KEYS key (those the sentence block names), `word_list` the old array field's text;
    `link_id_value` is its link ID field's text (the vocab block's `new_note_id_field`), from
    which its link ID follows (link_ids.link_id: that, else its note id); the word values are
    the vocab block's `word_kanjified_field`, `word_normal_field` and `word_reading_field`;
    `reps` is the sum of its cards' review counts."""

    note_id: int
    tags: Sequence[str] = ()
    fields: Mapping[str, str] = field(default_factory=dict)
    word_list: str = ""
    example_id: str = ""
    link_id_value: str = ""
    kanjified: str = ""
    normal: str = ""
    reading: str = ""
    reps: int = 0
    card_count: int = 1


@dataclass(frozen=True)
class SentenceRecord:
    """A note of the sentence type already in the collection: a rerun's, or the user's own.
    `link_id_value` is its link ID field's text (the sentence block's `sentence_id_field`)."""

    note_id: int
    tags: Sequence[str] = ()
    fields: Mapping[str, str] = field(default_factory=dict)
    word_list: str = ""
    link_id_value: str = ""


# --- the plan --------------------------------------------------------------------------------


@dataclass
class NewSentence:
    """A sentence note to add. `link_id`: the ID generated for it, which goes into its link ID
    field and is the example its vocab notes name. `fields` by config key: every SENTENCE_KEYS
    key the origin's record has, `word_list_field`, and `sentence_seen_count_field` when it is
    counted. `vocab_ids`: the selected vocab notes that get it as their example once it is
    added; `source_ids`: every source whose sentence it is, selected or not."""

    origin_id: int
    link_id: int
    fields: dict[str, str]
    tags: list[str]
    source_ids: list[int]
    vocab_ids: list[int]


@dataclass
class SentenceUpdate:
    """An existing sentence note the run joins vocab notes to. `link_id` is its link ID, the
    example its vocab notes name; `word_list` is its new array field text, None when the array
    stays as it is; `tags_to_add` holds none it has already."""

    note_id: int
    link_id: int
    word_list: Optional[str]
    tags_to_add: list[str]
    source_ids: list[int]
    vocab_ids: list[int]


@dataclass(frozen=True)
class Example:
    """A vocab note's example: `new_index` into `MigrationPlan.new_sentences`, or the `note_id`
    of an existing sentence note. The vocab note names it by that sentence note's `link_id`."""

    new_index: Optional[int] = None
    note_id: Optional[int] = None


# Each kind of case the report lists, in the order it lists them
CASES: dict[str, str] = {
    "not_vocab": "Selected, but not among the vocab notes read",
    "example_id_unknown": "Example id names no existing sentence note (migrated anyway)",
    "no_sentence_text": "Skipped: no sentence text (extraction and furigana fields empty)",
    "existing_no_text": "Existing sentence note with no sentence text (ignored)",
    "existing_duplicate": "Existing sentence note with the same sentence as an older one (only the"
    " older is joined)",
    "broken_array": "Source whose word array is unreadable",
    "keyed_by_furigana": "Source keyed by its furigana field (extraction field empty, no array)",
    "array_text_differs": "Origin whose word array is not of its sentence text (both kept)",
    "needs_extract": "New sentence note without a word array (tagged sentence-needs-extract)",
    "existing_no_array": "Joined an existing sentence note without a readable array: links not"
    " merged",
    "conflict": "Two links to different notes on one word: the first kept (tagged"
    " sentence-migration-conflict)",
    "links_lost": "Links of another source the alignment could not place",
    "combine_failed": "Another source's array could not be combined: its links not taken",
    "unknown_match_data": "Word whose match_data is in no known state (left as it was)",
    "placeholder_linked": "Legacy placeholder in a word array: linked to the vocab note whose ID"
    " field holds it",
    "placeholder_unheld": "Legacy placeholder in a word array that no vocab note holds: its word"
    " set to match",
    "placeholder_several": "Legacy placeholder in a word array that several vocab notes hold: left"
    " as it was",
    "several_cards": "Origin with more than one card: the seen count sums them",
    "linked": "Linked to its word by the link check",
    "link_no_array": "Not linked: its example has no readable word array",
    "link_text_differs": "Not linked: its extraction field is not its example's sentence",
    "link_no_bold": "Not linked: no <b> span in its extraction field",
    "link_no_element": "Not linked: no element of the example's array is its word",
    "link_bold_other_word": "Not linked: its <b> marks a word of neither its form nor its reading",
    "link_several": "Not linked: several elements could be its word",
    "link_other_note": "Not linked: its word's element links another note",
}


@dataclass
class Report:
    """What the plan decided that a person should see: counts, and for each case of CASES the
    note ids with a detail. A sentence note not added yet is named by its origin's id."""

    counts: dict[str, int] = field(default_factory=dict)
    cases: dict[str, list[tuple[int, str]]] = field(default_factory=dict)

    def add(self, case: str, note_id: int, detail: str = "") -> None:
        if case not in CASES:
            raise KeyError(f"Unknown report case {case!r}")
        self.cases.setdefault(case, []).append((note_id, detail))

    def count(self, name: str, increment: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + increment

    def note_ids(self, case: str) -> list[int]:
        return [note_id for note_id, _ in self.cases.get(case, [])]

    def text(self) -> str:
        lines = ["Counts"]
        lines += [f"  {name}: {value}" for name, value in self.counts.items()]
        for case, title in CASES.items():
            entries = self.cases.get(case)
            if not entries:
                continue
            lines += ["", f"{title} ({len(entries)})"]
            lines += [
                f"  {nid}: {detail}" if detail else f"  {nid}" for nid, detail in sorted(entries)
            ]
        return "\n".join(lines) + "\n"


@dataclass
class MigrationPlan:
    new_sentences: list[NewSentence]
    updates: list[SentenceUpdate]
    # Per selected vocab note given one: its example
    examples: dict[int, Example]
    # Per vocab note: the tags it loses (as it spells them), once its sentence note exists
    tags_to_remove: dict[int, list[str]]
    # Per note the plan changes (a vocab note above, or an existing sentence note whose array or
    # tags change) whose link ID field holds no link ID, empty or not: what the run writes there
    # along with its change, its note id. That is its link ID already (link_ids.link_id); in
    # the field it travels with the note (D17)
    link_id_fills: dict[int, str]
    report: Report

    def changed_with_new(self) -> list[list[int]]:
        """Per new sentence note, the vocab notes to change once it has been added: those it is
        the example of, then any other source of it that loses tags (an unselected origin losing
        the addon's own). A note of `tags_to_remove` with an example loses its tags with it."""
        changed = [list(new.vocab_ids) for new in self.new_sentences]
        source_of = {
            source_id: index
            for index, new in enumerate(self.new_sentences)
            for source_id in new.source_ids
        }
        for vocab_id in self.tags_to_remove:
            if vocab_id not in self.examples and vocab_id in source_of:
                changed[source_of[vocab_id]].append(vocab_id)
        return changed


# --- sentence text ---------------------------------------------------------------------------


def field_key(text: str) -> str:
    """The sentence key of a sentence field."""
    return match_flags.plain_text(strip_context_sentences(text))


def array_key(arr: list) -> str:
    """The sentence key of a word array: its top-level raw texts make up the sentence."""
    return "".join(match_flags.plain_text(elem[0]) for elem in arr)


def strip_bold(text: str) -> str:
    """The text without <b> and </b>: which word of a sentence note is bold depends on the
    vocab note showing it."""
    return _BOLD_TAG_RE.sub("", text)


def bold_span(text: str) -> Optional[tuple[int, int]]:
    """Where `text`'s <b> is, as a [start, end) range of its sentence key; None when it has
    none, or none that can be placed (unbalanced or nested tags).

    A <b> boundary inside a furigana group moves to the group's edge: in ` 無人</b>島[むじんとう]`
    the reading sits on the last kanji for the whole word, so 無人島 is what was marked. Several
    <b> spans give the range from the first to the last. Not text_map's work: it drops <b> as it
    reads, and its offsets run over the tokenizer's text, which has <k> groups in kana."""
    stripped = strip_context_sentences(text)
    if _OPEN in stripped or _CLOSE in stripped:
        return None
    marked = match_flags.TAG_RE.sub(
        "", _CLOSE_BOLD_RE.sub(_CLOSE, _OPEN_BOLD_RE.sub(_OPEN, stripped))
    )
    plain: list[str] = []
    # (mark, offset as written, offset moved to a group's edge)
    marks: list[tuple[str, int, int]] = []

    def run(chars: str) -> None:
        for char in chars:
            if char in (_OPEN, _CLOSE):
                marks.append((char, len(plain), len(plain)))
            elif char != " ":
                plain.append(char)

    position = 0
    for group in match_flags.FURIGANA_RE.finditer(marked):
        run(marked[position : group.start()])
        position = group.end()
        base = group.group(1).replace(_OPEN, "").replace(_CLOSE, "")
        start = len(plain)
        covered = (0, len(base))
        if not group.group(0).startswith(" "):
            tail = _KANJI_TAIL_RE.search(base)
            if tail:
                covered = (tail.start(), len(base))
        offset = 0
        for char in group.group(1):
            if char not in (_OPEN, _CLOSE):
                offset += 1
                continue
            moved = offset
            if covered[0] < offset < covered[1]:
                moved = covered[0] if char == _OPEN else covered[1]
            marks.append((char, start + offset, start + moved))
        plain.extend(base)
    run(marked[position:])

    # A mark inside a reading's brackets is gone, and the text must read as the key does, or
    # the offsets would point into some other sentence
    if len(marks) != marked.count(_OPEN) + marked.count(_CLOSE):
        return None
    if "".join(plain) != match_flags.plain_text(stripped):
        return None
    spans: list[tuple[int, int]] = []
    for index in range(0, len(marks), 2):
        pair = marks[index : index + 2]
        if len(pair) != 2 or pair[0][0] != _OPEN or pair[1][0] != _CLOSE:
            return None
        # An empty <b></b> the editor left behind marks nothing, wherever it sits
        if pair[0][1] < pair[1][1]:
            spans.append((pair[0][2], pair[1][2]))
    if not spans:
        return None
    return min(start for start, _ in spans), max(end for _, end in spans)


def _read_array(text: str) -> tuple[Optional[list], Optional[str]]:
    """(array, None) for a readable array holding text; (None, why) for an unreadable one;
    (None, None) for none. An array with no text (`[]`) holds nothing a sentence note would
    miss, so it counts as none: the sentence note then gets an empty field Extract words fills."""
    arr, problem = match_flags.read_word_array(text)
    if arr is not None and not array_key(arr):
        return None, None
    return arr, problem


def _norm(value: str) -> str:
    return unicodedata.normalize("NFC", match_flags.TAG_RE.sub("", value)).strip()


def _has_tag(tags: Iterable[str], tag: str) -> bool:
    # Anki's tags are case-insensitive
    return any(t.casefold() == tag.casefold() for t in tags)


def _add_tags(into: list[str], tags: Iterable[str], already: Iterable[str] = ()) -> None:
    """Append each tag that neither `into` nor `already` has, case aside: Anki's tags are
    case-insensitive, so two spellings are one tag, and the first one met is kept."""
    already = list(already)
    for tag in tags:
        if not _has_tag(into, tag) and not _has_tag(already, tag):
            into.append(tag)


def _falls_under(tag: str, listed: str) -> bool:
    """A `tag:` search's match of a tag name: the tag itself, case aside, or one of its
    children (`Show::ep01` under `Show`; `Showtime` is not). No wildcards: `_` is a wildcard in
    a search, and many tag names hold one."""
    tag, listed = tag.casefold(), listed.casefold()
    return tag == listed or tag.startswith(listed + "::")


def _tag_list(tags: Iterable[str], name: str) -> list[str]:
    """A config list of tag names, each once, blanks dropped (no tag holds a space). TypeError
    for anything else, which the op can show before the run starts."""
    if isinstance(tags, str):
        # Each letter would be a tag name of its own
        raise TypeError(f"{name} must be a list of tag names, not a string")
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise TypeError(f"{name} holds {tag!r}, which is not a tag name")
        if tag.strip():
            _add_tags(out, [tag.strip()])
    return out


def _example_note_id(value: str) -> Optional[int]:
    text = _norm(value)
    return int(text) if text.isdecimal() and text.isascii() else None


# --- planning --------------------------------------------------------------------------------


@dataclass(eq=False)
class _Vocab:
    record: VocabRecord
    # What an array links it by (link_ids.link_id)
    link_id: int
    extraction_key: str
    # Its sentence's key from its fields: the extraction field's, else the furigana field's
    text_key: str
    # Its legacy placeholders resolved (_resolve_placeholders)
    array: Optional[list]
    array_problem: Optional[str]
    # Report lines (case, detail) for the placeholders resolved in `array`, given only where
    # the array is copied into a sentence note
    placeholder_lines: list[tuple[str, str]]
    # A source's group key: its array's when it holds one, else text_key
    key: str
    forms: frozenset[str]
    reading: str

    @property
    def id(self) -> int:
        return self.record.note_id

    def is_word(self, elem: list) -> bool:
        """The element is this note's word: its dict_form is the word's kanjified or normal
        form and its reading the word's, compared in hiragana."""
        return self._has_form(elem) and self._has_reading(elem)

    def has_form_or_reading(self, elem: list) -> bool:
        """What an element found by place alone must have. A source's <b> marks its own word, but
        the array may spell or read it otherwise (食べる for 喰べる, むじんとう for むにんとう), so
        not both; one of them keeps a <b> on another word from linking that word to this note."""
        return self._has_form(elem) or self._has_reading(elem)

    def _has_form(self, elem: list) -> bool:
        return _norm(elem[2]) in self.forms

    def _has_reading(self, elem: list) -> bool:
        return bool(self.reading) and to_hiragana(_norm(elem[3])) == self.reading


def _read_vocab(
    records: Iterable[VocabRecord], placeholders: Mapping[str, list[int]]
) -> tuple[dict[int, _Vocab], set[int]]:
    """The sources by note id, and the ids of the notes the match op made, which the plan
    leaves alone: nothing of theirs is read, their sentence fields and any array included."""
    out: dict[int, _Vocab] = {}
    made_by_match_op: set[int] = set()
    for record in sorted(records, key=lambda r: r.note_id):
        if record.note_id in out or record.note_id in made_by_match_op:
            raise ValueError(f"Vocab note {record.note_id} is given twice")
        if _has_tag(record.tags, MATCH_OP_TAG):
            made_by_match_op.add(record.note_id)
            continue
        extraction_key = field_key(record.fields.get(_EXTRACTION_KEY, ""))
        text_key = extraction_key or field_key(record.fields.get(_FURIGANA_KEY, ""))
        arr, problem = _read_array(record.word_list)
        out[record.note_id] = _Vocab(
            record=record,
            link_id=link_id(record.link_id_value, record.note_id),
            extraction_key=extraction_key,
            text_key=text_key,
            array=arr,
            array_problem=problem,
            placeholder_lines=[] if arr is None else _resolve_placeholders(arr, placeholders),
            key=array_key(arr) if arr is not None else text_key,
            forms=frozenset(
                form for form in (_norm(record.kanjified), _norm(record.normal)) if form
            ),
            reading=to_hiragana(_norm(record.reading)),
        )
    return out, made_by_match_op


def _resolve_placeholders(arr: list, holders: Mapping[str, list[int]]) -> list[tuple[str, str]]:
    """Swap each legacy placeholder in `arr` for the link ID of the vocab note whose link ID
    field holds it (match_targets.resolve_placeholder_ids: none -> `["match"]`, several ->
    left), returning a report line (case, detail) per word it was on.

    A placeholder is the negative id a match op from before link IDs linked a new note by until
    the note was added; one still in an array is a note whose add failed, or whose field a hook
    wrote over before the id could be put in. Read as a link ID it links no note: the link
    check would take the word for another note's, and two sources linking one word, by the
    placeholder and by the note's ID, would be a conflict."""
    found = [
        (elem[2], match_flags.matched_note_id(elem)) for _, elem in match_flags.iter_words(arr)
    ]
    placeholders = [(form, held) for form, held in found if held is not None and held < 0]
    if not placeholders:
        return []
    match_targets.resolve_placeholder_ids(arr, lambda held: holders.get(str(held), []))
    lines: list[tuple[str, str]] = []
    for form, held in placeholders:
        notes = holders.get(str(held), [])
        if len(notes) == 1:
            lines.append(("placeholder_linked", f"{form}: {held} -> {notes[0]}"))
        elif not notes:
            lines.append(("placeholder_unheld", f"{form}: {held}"))
        else:
            held_by = ", ".join(str(note_id) for note_id in notes)
            lines.append(("placeholder_several", f"{form}: {held}, held by {held_by}"))
    return lines


@dataclass(eq=False)
class _Entry:
    elem: list
    # Its [start, end) in the sentence key; None where its parent's sub-words do not add up
    # to the parent's text, so their places are not known
    span: Optional[tuple[int, int]]


@dataclass(eq=False)
class _Target:
    """A sentence note of the plan: one to make from a group of sources, or an existing one."""

    key: str
    # Which of several is the oldest: the origin's id, or the existing note's
    age_id: int
    existing: Optional[SentenceRecord] = None
    # An existing note's link ID; a new one's is generated once it is known to be made
    link_id: Optional[int] = None
    origin: Optional[_Vocab] = None
    sources: list[_Vocab] = field(default_factory=list)
    # What its array field will hold: a copy combined from the sources' arrays, or None
    array: Optional[list] = None
    # An existing note's array as read, to tell whether it changed
    original: Optional[list] = None
    # A new note whose origin's array is unreadable: that text, carried as it is
    broken_text: Optional[str] = None
    conflict: bool = False
    # Report lines, given only if a selected note needs this sentence note
    pending: list[tuple[str, int, str]] = field(default_factory=list)
    vocab_ids: list[int] = field(default_factory=list)
    entries: Optional[list[_Entry]] = None

    @property
    def label(self) -> str:
        if self.existing is not None:
            return f"sentence note {self.existing.note_id}"
        assert self.origin is not None
        return f"the sentence of {self.origin.id}"

    @property
    def report_id(self) -> int:
        return self.existing.note_id if self.existing is not None else self.age_id


def plan_migration(
    vocab_notes: Iterable[VocabRecord],
    sentence_notes: Iterable[SentenceRecord],
    selected: Collection[int],
    *,
    count_seen: bool,
    move_tags: Iterable[str] = (),
    copy_tags: Iterable[str] = (),
) -> MigrationPlan:
    """The plan for moving the selected vocab notes' sentences to sentence notes.

    `vocab_notes` is every vocab note of the type, not only the selected: an unselected note's
    array may link a selected one, and a group's sentence note is made from its origin whether
    or not the origin is selected. The notes the match op made are among them for their link
    IDs alone, which the arrays link them by and a legacy placeholder resolves to: the plan
    leaves those notes alone, selected or not. `sentence_notes` is every note of the sentence
    type. `count_seen`: the sentence block names `sentence_seen_count_field`.

    `move_tags`, `copy_tags`: the sentence block's `migration_move_tags` and
    `migration_copy_tags`, the user's tags that go to the sentence note (`_UserTags.carry`).
    A listed tag also covers its `::` children, case aside, as a `tag:` search does. Raises
    TypeError when either is not a list of strings.

    Notes are linked by link ID (link_ids.py), so every link is read as one: an array's
    element names the vocab note holding that ID, an example id the sentence note holding it.
    A new sentence note gets an ID generated from its origin's (`sentence|<origin's link ID>`,
    so the same records plan the same IDs) that no sentence note holds, which its vocab notes
    then name. The op refuses to run while two notes of either type hold one ID; given such
    records anyway, an ID names its oldest holder, as `LinkIndex` answers.
    """
    user_tags = _UserTags(
        _tag_list(move_tags, MOVE_TAGS_KEY), _tag_list(copy_tags, COPY_TAGS_KEY)
    )
    report = Report()
    vocab_records = list(vocab_notes)
    sentence_records = list(sentence_notes)
    vocab_index = LinkIndex.from_rows((r.note_id, r.link_id_value) for r in vocab_records)
    # Every sentence note's ID, those of notes no group joins included: a new one's must be
    # none of them
    sentence_index = LinkIndex.from_rows((r.note_id, r.link_id_value) for r in sentence_records)
    placeholders = placeholder_holders(vocab_index)
    targets, by_key, existing_ids = _existing_targets(sentence_records, placeholders, report)
    vocab, made_by_match_op = _read_vocab(vocab_records, placeholders)
    migrated = {
        nid for nid, v in vocab.items() if _example_note_id(v.record.example_id) in existing_ids
    }
    selected_ids = sorted(set(selected))
    report.count("vocab notes read", len(vocab) + len(made_by_match_op))
    report.count("sources", len(vocab))
    report.count(f"made by the match op (tagged {MATCH_OP_TAG}), left alone", len(made_by_match_op))
    report.count("selected", len(selected_ids))

    # Sources by sentence key. A source already given a sentence note is done: its array went
    # into that note when it was made, and merging it again would undo what the user changed
    # there since
    groups: dict[str, list[_Vocab]] = {}
    for v in vocab.values():
        if v.id not in migrated and v.text_key:
            groups.setdefault(v.key, []).append(v)
    for key, sources in groups.items():
        target = by_key.get(key)
        if target is None:
            target = _Target(key=key, age_id=_origin(sources).id)
            targets.append(target)
            by_key[key] = target
        target.sources = sources
        _combine_sources(target)

    _assign(vocab, made_by_match_op, selected_ids, migrated, by_key, report)
    for target in targets:
        for vocab_id in target.vocab_ids:
            _link_check(target, vocab[vocab_id], report)
    return _plan(targets, vocab, sentence_index, count_seen, user_tags, report)


def _existing_targets(
    sentence_notes: Iterable[SentenceRecord],
    placeholders: Mapping[str, list[int]],
    report: Report,
) -> tuple[list[_Target], dict[str, _Target], set[int]]:
    """The existing sentence notes keyed the same way as the sources, first, so that a group with
    the same sentence joins its note rather than making another; and the link IDs of all of
    them, those without text included (a vocab note naming one has been migrated all the same).
    Their arrays' legacy placeholders are resolved as the sources' are, so that links compare
    alike; the array is written only if the note is joined."""
    targets: list[_Target] = []
    by_key: dict[str, _Target] = {}
    ids: set[int] = set()
    for record in sorted(sentence_notes, key=lambda r: r.note_id):
        held = link_id(record.link_id_value, record.note_id)
        ids.add(held)
        arr, _problem = _read_array(record.word_list)
        key = array_key(arr) if arr is not None else _text_key(record.fields)
        if not key:
            report.add("existing_no_text", record.note_id)
            continue
        array = copy.deepcopy(arr)
        placeholder_lines = [] if array is None else _resolve_placeholders(array, placeholders)
        target = _Target(
            key=key,
            age_id=record.note_id,
            existing=record,
            link_id=held,
            array=array,
            original=arr,
            pending=[(case, record.note_id, detail) for case, detail in placeholder_lines],
        )
        targets.append(target)
        older = by_key.get(key)
        if older is not None:
            report.add("existing_duplicate", record.note_id, older.label)
        else:
            by_key[key] = target
    return targets, by_key, ids


def _text_key(fields: Mapping[str, str]) -> str:
    return field_key(fields.get(_EXTRACTION_KEY, "")) or field_key(fields.get(_FURIGANA_KEY, ""))


def _origin(sources: list[_Vocab]) -> _Vocab:
    """The source a group's sentence note is made from: the oldest holding an array, else the
    oldest holding an unreadable one (its text is then carried, never dropped), else the oldest.
    `sources` is in id order."""
    with_array = [s for s in sources if s.array is not None]
    with_broken = [s for s in sources if s.array_problem is not None]
    return (with_array or with_broken or sources)[0]


def _combine_sources(target: _Target) -> None:
    """The sentence note's array: its origin's (an existing note's own, or the group's origin's)
    with the other sources' match_data combined into it."""
    if target.existing is None:
        origin = target.origin = _origin(target.sources)
        if origin.array is not None:
            target.array = copy.deepcopy(origin.array)
            _placeholders_copied(target, origin)
            if array_key(origin.array) != origin.extraction_key:
                target.pending.append(("array_text_differs", origin.id, ""))
        elif origin.array_problem is not None:
            target.broken_text = origin.record.word_list
            target.pending.append(
                ("broken_array", origin.id, f"{origin.array_problem}; carried as it is")
            )
    for source in target.sources:
        if source.array is None and not source.extraction_key:
            target.pending.append(("keyed_by_furigana", source.id, ""))
        if source is target.origin:
            continue
        if source.array is None:
            if source.array_problem is not None:
                target.pending.append(
                    (
                        "broken_array",
                        source.id,
                        f"{source.array_problem}; its links are not in {target.label}",
                    )
                )
        elif target.array is None:
            target.pending.append(("existing_no_array", source.id, target.label))
        else:
            _combine_into(target, source)


def _combine_into(target: _Target, source: _Vocab) -> None:
    """Combine one more source's match_data into the target's array, all or nothing."""
    assert target.array is not None and source.array is not None
    base = target.array
    # merge_arrays writes into `new` and hands back elements of `old`; both are copies, so
    # neither the source's array nor the target's changes unless the whole combination holds
    try:
        aligned = merge.merge_arrays(copy.deepcopy(source.array), copy.deepcopy(base))
    except ValueError as e:
        target.pending.append(("combine_failed", source.id, f"{e} ({target.label})"))
        return
    combined = copy.deepcopy(base)
    events: list[tuple[str, str]] = []
    try:
        _combine_level(combined, aligned.array, events)
        # As merge_arrays checks its own result: only a bug can fail this, and the check is
        # what keeps such a bug out of the field
        if _raw(combined) != _raw(base):
            raise ValueError("the combined array does not reconstruct the sentence")
    except ValueError as e:
        target.pending.append(("combine_failed", source.id, f"{e} ({target.label})"))
        return
    target.array = combined
    _placeholders_copied(target, source)
    for case, detail in events:
        target.pending.append((case, source.id, f"{detail} ({target.label})"))
        if case == "conflict":
            target.conflict = True
    still_linked = {
        match_flags.matched_note_id(elem) for _, elem in match_flags.iter_words(combined)
    }
    lost = sorted({nid for nid in aligned.lost if nid not in still_linked})
    if lost:
        target.pending.append(
            ("links_lost", source.id, f"{', '.join(map(str, lost))} ({target.label})")
        )


def _placeholders_copied(target: _Target, source: _Vocab) -> None:
    # Only a source whose array the sentence note takes: one whose array is not taken has had
    # its placeholders resolved for nothing
    for case, detail in source.placeholder_lines:
        target.pending.append((case, source.id, f"{detail} ({target.label})"))


def _raw(arr: list) -> str:
    return "".join(elem[0] for elem in arr)


def _combine_level(base: list, other: list, events: list[tuple[str, str]]) -> None:
    """Combine `other`'s match_data into `base` element by element, the two being one structure
    (merge_arrays aligned `other` onto a copy of `base`); raises ValueError where they are not."""
    if len(base) != len(other):
        raise ValueError("the aligned array is not of the same structure")
    for base_elem, other_elem in zip(base, other):
        if len(base_elem) != len(other_elem) or base_elem[0] != other_elem[0]:
            raise ValueError("the aligned array is not of the same text")
        if len(base_elem) > 1:
            _combine_match_data(base_elem, other_elem, events)
            _combine_level(base_elem[5], other_elem[5], events)


# Of two unlinked states the more judged wins; any link beats all three
_RANK = {MatchState.UNJUDGED: 0, MatchState.DONT_MATCH: 1, MatchState.MATCH: 2}


def _combine_match_data(base: list, other: list, events: list[tuple[str, str]]) -> None:
    """A link beats ["match"] beats ["dontmatch"] beats [], a rated link the same link unrated;
    on a tie `base`, which holds the origin's, stays. Two links to different notes: base's stays
    and the conflict is an event."""
    try:
        base_state = match_flags.match_state(base)
    except ValueError:
        events.append(("unknown_match_data", f"{base[2]} {base[4]!r} kept"))
        return
    try:
        other_state = match_flags.match_state(other)
    except ValueError:
        events.append(("unknown_match_data", f"{other[2]} {other[4]!r} not taken"))
        return
    base_id = match_flags.matched_note_id(base)
    other_id = match_flags.matched_note_id(other)
    if base_id is not None and other_id is not None:
        if base_id != other_id:
            events.append(("conflict", f"{base[2]}: {base_id} kept, {other_id} not"))
        elif base_state is MatchState.LINKED and other_state is MatchState.RATED:
            base[4] = list(other[4])
        return
    if base_id is not None:
        return
    if other_id is not None or _RANK[other_state] > _RANK[base_state]:
        base[4] = list(other[4])


def _assign(
    vocab: dict[int, _Vocab],
    made_by_match_op: set[int],
    selected: list[int],
    migrated: set[int],
    by_key: dict[str, _Target],
    report: Report,
) -> None:
    """Give each selected source the sentence note of its own sentence as its example. A
    target is made or changed only because a selected note is assigned to it.

    A selected note the match op made is skipped. Its sentence fields hold a copy of the
    sentence its word was found in, not always a whole one (a new-meaning copy keeps the copied
    note's extraction field), and a sentence only such copies hold is not one to keep: nothing
    of the note is read, and the arrays linking it stay as they are."""
    for nid in selected:
        v = vocab.get(nid)
        if v is None:
            if nid in made_by_match_op:
                # Counted, not listed: a selection of the whole type holds thousands
                report.count("skipped: made by the match op")
            else:
                report.add("not_vocab", nid)
            continue
        if nid in migrated:
            # Counted, not listed: on a rerun that is nearly every note selected
            report.count("skipped: already migrated")
            continue
        if _norm(v.record.example_id):
            report.add("example_id_unknown", nid, _norm(v.record.example_id))
        if not v.text_key:
            report.add("no_sentence_text", nid)
            continue
        by_key[v.key].vocab_ids.append(nid)


def _entries(arr: list) -> list[_Entry]:
    """Every word element with its range of the sentence key: the top-level raw texts make up
    the sentence and a parent's sub-words its raw text (README "Structure")."""
    out: list[_Entry] = []

    def walk(elems: list, start: Optional[int]) -> None:
        position = start
        for elem in elems:
            length = len(match_flags.plain_text(elem[0]))
            if len(elem) > 1:
                span = None if position is None else (position, position + length)
                out.append(_Entry(elem, span))
                subs_add_up = "".join(
                    match_flags.plain_text(sub[0]) for sub in elem[5]
                ) == match_flags.plain_text(elem[0])
                walk(elem[5], position if subs_add_up else None)
            if position is not None:
                position += length

    walk(arr, 0)
    return out


def _link_check(target: _Target, v: _Vocab, report: Report) -> None:
    """Link V's word in its example's array to V's link ID when nothing there links V yet, by
    V's <b>."""
    arr = target.array
    if arr is None:
        report.add("link_no_array", v.id, target.label)
        return
    if any(
        match_flags.matched_note_id(elem) == v.link_id for _, elem in match_flags.iter_words(arr)
    ):
        return
    if v.extraction_key != target.key:
        report.add("link_text_differs", v.id, target.label)
        return
    span = bold_span(v.record.fields.get(_EXTRACTION_KEY, ""))
    if span is None:
        report.add("link_no_bold", v.id, target.label)
        return
    if target.entries is None:
        target.entries = _entries(arr)
    elem, how, why = _pick_element(target.entries, span, v)
    word = f"{_norm(v.record.kanjified) or _norm(v.record.normal)}[{_norm(v.record.reading)}]"
    if elem is None:
        report.add(how, v.id, f"{word} in {target.label}" + (f"; {why}" if why else ""))
        return
    try:
        state = match_flags.match_state(elem)
    except ValueError:
        report.add("unknown_match_data", v.id, f"{elem[2]} {elem[4]!r} ({target.label})")
        return
    if state in (MatchState.LINKED, MatchState.RATED):
        linked = match_flags.matched_note_id(elem)
        report.add("link_other_note", v.id, f"{elem[2]} links {linked} ({target.label})")
        return
    elem[4] = [v.link_id]
    report.add("linked", v.id, f"{elem[2]}[{elem[3]}] {how} ({target.label})")
    report.count("links set by the link check")


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _pick_element(
    entries: list[_Entry], span: tuple[int, int], v: _Vocab
) -> tuple[Optional[list], str, str]:
    """(the element, how it was found, ""), or (None, the report case, what else its line
    says). In order: an element at the <b> that is V's word; else, since a source's <b> marks
    its own word, the element at exactly the <b>, else the smallest around it, when it has V's
    form or reading."""
    words = [e for e in entries if v.is_word(e.elem)]
    at_bold = [e for e in words if e.span is not None and _overlaps(e.span, span)]
    # A word whose place is unknown may be at the <b> too
    unplaced = [e for e in words if e.span is None]
    if at_bold:
        if len(at_bold) == 1 and not unplaced:
            return at_bold[0].elem, "at its <b>", ""
        return None, "link_several", ""
    # By place alone: only where every element's place is known
    if any(e.span is None for e in entries):
        return None, "link_no_element", ""
    equal = [e for e in entries if e.span == span]
    if equal:
        found, how = equal, "at exactly its <b>"
    else:
        around = [
            e
            for e in entries
            if e.span is not None and e.span[0] <= span[0] and span[1] <= e.span[1]
        ]
        if not around:
            return None, "link_no_element", ""
        smallest = min(e.span[1] - e.span[0] for e in around if e.span is not None)
        found = [e for e in around if e.span is not None and e.span[1] - e.span[0] == smallest]
        how = "around its <b>"
    if len(found) > 1:
        return None, "link_several", ""
    elem = found[0].elem
    # A <b> on another word: an untagged copy's, still on its source's word, or one moved by
    # hand. Linking there would bold the wrong word on the card for good; a link missed here is
    # reported, and the match op can still make it
    if not v.has_form_or_reading(elem):
        return None, "link_bold_other_word", f"its <b> marks {elem[2]}[{elem[3]}]"
    return elem, how, ""


@dataclass
class _UserTags:
    """The user's tags that go to the sentence note: `move` ones are about the sentence alone
    and leave the vocab notes, `copy` ones are about the word too and stay on them. A tag under
    both lists moves. Per listed tag, the vocab notes it was moved or copied from are counted."""

    move: list[str]
    copy: list[str]
    moved_from: dict[str, int] = field(init=False)
    copied_from: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        # Every listed tag, so that one matching nothing (a typo) shows as 0
        self.moved_from = dict.fromkeys(self.move, 0)
        self.copied_from = dict.fromkeys(self.copy, 0)

    def carry(self, target: _Target) -> tuple[list[str], dict[int, list[str]]]:
        """The tags the target's sentence note gets, as the oldest note carrying one spells it,
        and per vocab note the tags it loses.

        A tag moves from each selected note assigned to the target, and is copied from each of
        the target's sources, selected or not, since the sentence is theirs; every note assigned
        is one of them. A source not selected loses nothing and gives no tag to move: the run
        changes no vocab note the user left out, and the run that selects it joins it to this
        sentence note and moves its tags then. A note the match op made is never one of them,
        and gives and loses nothing."""
        selected = set(target.vocab_ids)
        added: list[str] = []
        removed: dict[int, list[str]] = {}
        for source in sorted(target.sources, key=lambda s: s.id):
            nid = source.id
            moved_under: set[str] = set()
            copied_under: set[str] = set()
            for tag in source.record.tags:
                under = [listed for listed in self.move if _falls_under(tag, listed)]
                if under:
                    if nid in selected:
                        _add_tags(added, [tag])
                        _add_tags(removed.setdefault(nid, []), [tag])
                        moved_under.update(under)
                    continue
                under = [listed for listed in self.copy if _falls_under(tag, listed)]
                if under:
                    _add_tags(added, [tag])
                    copied_under.update(under)
            for listed in moved_under:
                self.moved_from[listed] += 1
            for listed in copied_under:
                self.copied_from[listed] += 1
        return added, removed


def _plan(
    targets: list[_Target],
    vocab: Mapping[int, _Vocab],
    sentence_index: LinkIndex,
    count_seen: bool,
    user_tags: _UserTags,
    report: Report,
) -> MigrationPlan:
    new_sentences: list[NewSentence] = []
    updates: list[SentenceUpdate] = []
    examples: dict[int, Example] = {}
    tags_to_remove: dict[int, list[str]] = {}
    link_id_fills: dict[int, str] = {}
    needed = [t for t in targets if t.vocab_ids]
    for target in needed:
        for case, note_id, detail in target.pending:
            report.add(case, note_id, detail)

    for target in sorted((t for t in needed if t.existing is None), key=lambda t: t.age_id):
        origin = target.origin
        assert origin is not None
        fields = {
            key: value if key == _AUDIO_KEY else strip_bold(value)
            for key, value in origin.record.fields.items()
            if key in SENTENCE_KEYS
        }
        if target.array is not None:
            fields[WORD_LIST_KEY] = match_flags.format_word_array(target.array)
        else:
            fields[WORD_LIST_KEY] = target.broken_text or ""
        if count_seen:
            fields[SEEN_COUNT_KEY] = str(origin.record.reps)
            if origin.record.card_count > 1:
                report.add("several_cards", origin.id, f"{origin.record.card_count} cards")
        moved = [t for t in origin.record.tags if any(t.casefold() == m for m in MOVED_TAGS)]
        tags = list(moved)
        if target.broken_text is not None and not _has_tag(tags, INVALID_ARRAY_TAG):
            tags.append(INVALID_ARRAY_TAG)
        if target.array is None and target.broken_text is None:
            tags.append(NEEDS_EXTRACT_TAG)
            report.add("needs_extract", origin.id)
        if target.conflict:
            tags.append(CONFLICT_TAG)
        if moved:
            tags_to_remove.setdefault(origin.id, []).extend(moved)
        carried, lost = user_tags.carry(target)
        _add_tags(tags, carried)
        for vocab_id, vocab_tags in lost.items():
            _add_tags(tags_to_remove.setdefault(vocab_id, []), vocab_tags)
        index = len(new_sentences)
        new_sentences.append(
            NewSentence(
                origin_id=origin.id,
                # Keyed by its origin's link ID, so that the same records plan the same IDs, and
                # none that a sentence note holds or that went to an older origin before it
                link_id=sentence_index.new_link_id(f"sentence|{origin.link_id}"),
                fields=fields,
                tags=tags,
                source_ids=[s.id for s in target.sources],
                vocab_ids=list(target.vocab_ids),
            )
        )
        for vocab_id in target.vocab_ids:
            examples[vocab_id] = Example(new_index=index)

    for target in (t for t in needed if t.existing is not None):
        existing = target.existing
        assert existing is not None and target.link_id is not None
        word_list = None
        if target.array is not None and target.array != target.original:
            word_list = match_flags.format_word_array(target.array)
        tags_to_add: list[str] = []
        _add_tags(tags_to_add, [CONFLICT_TAG] if target.conflict else [], existing.tags)
        carried, lost = user_tags.carry(target)
        _add_tags(tags_to_add, carried, existing.tags)
        for vocab_id, vocab_tags in lost.items():
            _add_tags(tags_to_remove.setdefault(vocab_id, []), vocab_tags)
        updates.append(
            SentenceUpdate(
                note_id=existing.note_id,
                link_id=target.link_id,
                word_list=word_list,
                tags_to_add=tags_to_add,
                source_ids=[s.id for s in target.sources],
                vocab_ids=list(target.vocab_ids),
            )
        )
        for vocab_id in target.vocab_ids:
            examples[vocab_id] = Example(note_id=existing.note_id)
        # Only a note the run saves anyway: filling the field alone would be a write of its own
        # to every sentence note a rerun joins
        written = word_list is not None or bool(tags_to_add)
        if written and parse_link_id(existing.link_id_value) is None:
            link_id_fills[existing.note_id] = str(existing.note_id)

    for vocab_id in sorted({*examples, *tags_to_remove}):
        if parse_link_id(vocab[vocab_id].record.link_id_value) is None:
            link_id_fills[vocab_id] = str(vocab_id)

    report.count("sentence notes to add", len(new_sentences))
    report.count("existing sentence notes joined", len(updates))
    report.count(
        "existing sentence notes whose array changes",
        sum(1 for u in updates if u.word_list is not None),
    )
    report.count(
        "examples: new sentence notes", sum(len(s.vocab_ids) for s in new_sentences)
    )
    report.count("examples: existing sentence notes", sum(len(u.vocab_ids) for u in updates))
    report.count(
        "vocab notes whose ID field gets their note id",
        sum(1 for note_id in link_id_fills if note_id in vocab),
    )
    report.count(
        "existing sentence notes whose ID field gets their note id",
        sum(1 for note_id in link_id_fills if note_id not in vocab),
    )
    for case, name in (
        ("placeholder_linked", "legacy placeholders linked to the note holding them"),
        ("placeholder_unheld", "legacy placeholders no note holds, set to match"),
        ("placeholder_several", "legacy placeholders several notes hold, left"),
    ):
        report.count(name, len(report.cases.get(case, [])))
    for listed, notes in user_tags.moved_from.items():
        report.count(f'tag "{listed}" moved from vocab notes', notes)
    for listed, notes in user_tags.copied_from.items():
        report.count(f'tag "{listed}" copied from vocab notes', notes)
    return MigrationPlan(
        new_sentences=new_sentences,
        updates=updates,
        examples=examples,
        tags_to_remove=tags_to_remove,
        link_id_fills=link_id_fills,
        report=report,
    )


# --- pre-flight ------------------------------------------------------------------------------


def _named_fields(block: Mapping[str, Any], keys: Iterable[str]) -> list[tuple[str, str]]:
    """(key, field name) for each of `keys` the block names a field for."""
    return [(key, block[key]) for key in keys if isinstance(block.get(key), str) and block[key]]


def _missing(wanted: Iterable[tuple[str, str]], fields: Collection[str]) -> list[str]:
    return [f'"{name}" ({key})' for key, name in wanted if name not in fields]


def preflight_error(
    config: Mapping[str, Any],
    selected_types: Collection[str],
    note_type_fields: Mapping[str, Collection[str]],
    has_deck: Callable[[str], bool],
) -> Optional[str]:
    """Why the migration cannot run over notes of `selected_types`, for the user; None when it
    can. `note_type_fields`: every note type of the collection by name, with its field names;
    `has_deck(name)`: whether the collection has that deck.

    Checked before the run starts, and again by the run, which a script starts without the
    menu: everything the run reads and writes is named in the settings, and a name that finds
    nothing would fail it note by note (a field the copy writes), or plan wrongly without a
    word (an old field read as empty is a vocab note with no sentence). The old sentence fields
    are read off the vocab notes by the sentence block's names, since by now the vocab block
    names none of them, so the vocab type must still have each under that name. What only the
    notes can say, a link ID two of them hold, is the op's to check (and a link ID field named
    like a search keyword, which link_lookup refuses)."""
    if not selected_types:
        return "No notes are selected."
    if len(selected_types) > 1:
        names = ", ".join(f'"{name}"' for name in sorted(selected_types))
        return (
            f"The selected notes are of several note types ({names}): select the notes of the"
            " vocab note type only."
        )
    [vocab_type] = selected_types
    error = layout_error(config, vocab_type)
    if error:
        return error
    if not is_vocab_type(config, vocab_type):
        return (
            f'"{vocab_type}" is not a vocab note type (its settings name no word_sort_field):'
            " select the vocab notes whose sentences are to move."
        )
    sentence_type = sentence_type_of(config, vocab_type)
    if sentence_type == vocab_type:
        return (
            f'"{vocab_type}" names no sentence_note_type in the settings: the sentences need a'
            " note type of their own to move to (two note types, see config.md)."
        )
    vocab_block, sentence_block = config[vocab_type], config[sentence_type]

    sentence_fields = note_type_fields.get(sentence_type)
    if sentence_fields is None:
        return f'The sentence note type "{sentence_type}" is not in the collection.'
    missing = _missing(
        _named_fields(sentence_block, [key for key in sentence_block if key.endswith("_field")]),
        sentence_fields,
    )
    if missing:
        return (
            f'The sentence note type "{sentence_type}" has no field {", ".join(missing)}, as'
            " its settings name."
        )
    if not _named_fields(sentence_block, [SENTENCE_ID_KEY]):
        return (
            f'"{sentence_type}" has no {SENTENCE_ID_KEY} in the settings: the migration writes'
            " each new sentence note's link ID into that field, the ID its vocab notes name"
            " their example by."
        )

    if not _named_fields(vocab_block, [EXAMPLE_ID_KEY]):
        return (
            f'"{vocab_type}" has no {EXAMPLE_ID_KEY} in the settings: the migration writes the id'
            " of each vocab note's example sentence note into that field."
        )
    if not _named_fields(vocab_block, [VOCAB_ID_KEY]):
        return (
            f'"{vocab_type}" has no {VOCAB_ID_KEY} in the settings: the migration writes each'
            " vocab note's link ID into that field, the ID the word arrays link it by."
        )
    unset = [key for key in WORD_KEYS if not _named_fields(vocab_block, [key])]
    if unset:
        return (
            f'"{vocab_type}" has no {", ".join(unset)} in the settings: the migration reads the'
            " vocab notes' word from them to link it in the sentence's word array."
        )
    vocab_fields = note_type_fields.get(vocab_type, ())
    # The fields copy_example writes: the id, and each sentence key both blocks name
    copied = [key for key, _ in _named_fields(sentence_block, SENTENCE_KEYS)]
    missing = _missing(
        _named_fields(vocab_block, [EXAMPLE_ID_KEY, VOCAB_ID_KEY, *WORD_KEYS, *copied]),
        vocab_fields,
    )
    if missing:
        return (
            f'The vocab note type "{vocab_type}" has no field {", ".join(missing)}, as its'
            " settings name."
        )
    old = _named_fields(sentence_block, [*SENTENCE_KEYS, WORD_LIST_KEY])
    missing = _missing(old, vocab_fields)
    if missing:
        return (
            f'The vocab note type "{vocab_type}" has no field {", ".join(missing)}. The'
            f' migration reads the sentences off the vocab notes by the names "{sentence_type}"'
            " gives its fields, so the vocab notes must keep their old sentence fields, named"
            f' as on "{sentence_type}", until it has run.'
        )

    for key in (MOVE_TAGS_KEY, COPY_TAGS_KEY):
        listed = sentence_block.get(key, [])
        if not isinstance(listed, list):
            return f'{key} of "{sentence_type}" must be a list of tag names, not {listed!r}.'
        try:
            _tag_list(listed, key)
        except TypeError as e:
            return f'The settings of "{sentence_type}": {e}.'

    deck = sentence_block.get("insert_deck")
    if isinstance(deck, str) and deck and not has_deck(deck):
        return (
            f'The deck "{deck}" (insert_deck of "{sentence_type}") is not in the collection:'
            " every sentence note would fail to be added."
        )
    return None
