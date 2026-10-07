"""Planning "Move sentences to sentence notes": the sentence notes to make from the vocab notes
that hold the sentences today, the existing ones to join, and the example each vocab note gets.

The op reads every vocab note of the type, the sentence notes already there and the selection,
and hands them here as plain records; everything it writes follows from the plan this returns.
The rules are where a mistake costs: a wrong link puts the wrong word in bold on a card,
silently and for good, while a case the plan cannot decide is listed in the report and left to
a person. So wherever a rule cannot decide, the plan links nothing and reports it.

Records are keyed by config key (`note_roles.SENTENCE_KEYS`, `word_list_field`), never by field
name: by the time the migration runs the vocab block names no sentence fields, and the op reads
the vocab notes' old ones by the sentence block's names. Free of anki and aqt, so all of it is
tested on hand-written arrays.

Terms, as in the plan of this work: a *source* is a vocab note without `new_matched_jp_word`
(every unique sentence is in one); a *dependent* is a tagged one, a copy the match op made of
its source's sentence, which only gets an example. A *sentence key* is a sentence's plain text
(`match_flags.plain_text` after `strip_context_sentences`); an array's is the plain text of its
top-level raw texts, which is the key of the field it was generated from.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional

from ..html_stripping import strip_context_sentences
from ..kana_conv import to_hiragana
from ..note_roles import SENTENCE_KEYS
from ..word_array import match_flags, merge
from ..word_array.match_flags import MatchState

# The tags this concerns. The first three are the addon's own, spelled as the ops that set them
# spell them (each hardcodes its own)
DEPENDENT_TAG = "new_matched_jp_word"
INVALID_ARRAY_TAG = "invalid_word_list_json"
KANJIFY_MISMATCH_TAG = "kanjify_sentence_mismatch"
NEEDS_EXTRACT_TAG = "sentence-needs-extract"
CONFLICT_TAG = "sentence-migration-conflict"
# The addon's tags that are about the sentence: they move from the vocab note a sentence note is
# made from to that sentence note
MOVED_TAGS = (KANJIFY_MISMATCH_TAG, INVALID_ARRAY_TAG)

WORD_LIST_KEY = "word_list_field"
SEEN_COUNT_KEY = "sentence_seen_count_field"
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
    the word values are the vocab block's `word_kanjified_field`, `word_normal_field` and
    `word_reading_field`; `reps` is the sum of its cards' review counts."""

    note_id: int
    tags: Sequence[str] = ()
    fields: Mapping[str, str] = field(default_factory=dict)
    word_list: str = ""
    example_id: str = ""
    kanjified: str = ""
    normal: str = ""
    reading: str = ""
    reps: int = 0
    card_count: int = 1


@dataclass(frozen=True)
class SentenceRecord:
    """A note of the sentence type already in the collection: a rerun's, or the user's own."""

    note_id: int
    tags: Sequence[str] = ()
    fields: Mapping[str, str] = field(default_factory=dict)
    word_list: str = ""


# --- the plan --------------------------------------------------------------------------------


@dataclass
class NewSentence:
    """A sentence note to add. `fields` by config key: every SENTENCE_KEYS key the origin's
    record has, `word_list_field`, and `sentence_seen_count_field` when it is counted.
    `vocab_ids`: the selected vocab notes that get it as their example once it is added;
    `source_ids`: every source whose sentence it is, selected or not."""

    origin_id: int
    fields: dict[str, str]
    tags: list[str]
    source_ids: list[int]
    vocab_ids: list[int]


@dataclass
class SentenceUpdate:
    """An existing sentence note the run joins vocab notes to. `word_list` is its new array
    field text, None when the array stays as it is; `tags_to_add` holds none it has already."""

    note_id: int
    word_list: Optional[str]
    tags_to_add: list[str]
    source_ids: list[int]
    vocab_ids: list[int]


@dataclass(frozen=True)
class Example:
    """A vocab note's example: `new_index` into `MigrationPlan.new_sentences`, or the `note_id`
    of an existing sentence note."""

    new_index: Optional[int] = None
    note_id: Optional[int] = None


# Each kind of case the report lists, in the order it lists them
CASES: dict[str, str] = {
    "not_vocab": "Selected, but not among the vocab notes read",
    "example_id_unknown": "Example id names no existing sentence note (migrated anyway)",
    "no_sentence_text": "Skipped: no sentence text (extraction and furigana fields empty)",
    "dependent_no_example": "Dependent with no example: no sentence links it or has its text",
    "dependent_several": "Dependent linked from several sentences, none with its text: the oldest"
    " is its example",
    "dependent_other_sentence": "Dependent whose example (the sentence linking it) is not its own"
    " sentence text",
    "dependent_array": "Dependent holding a word array of its own (not used)",
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
    report: Report


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
    dependent: bool
    extraction_key: str
    # Its sentence's key from its fields: the extraction field's, else the furigana field's
    text_key: str
    array: Optional[list]
    array_problem: Optional[str]
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


def _read_vocab(records: Iterable[VocabRecord]) -> dict[int, _Vocab]:
    out: dict[int, _Vocab] = {}
    for record in sorted(records, key=lambda r: r.note_id):
        if record.note_id in out:
            raise ValueError(f"Vocab note {record.note_id} is given twice")
        extraction_key = field_key(record.fields.get(_EXTRACTION_KEY, ""))
        text_key = extraction_key or field_key(record.fields.get(_FURIGANA_KEY, ""))
        dependent = _has_tag(record.tags, DEPENDENT_TAG)
        # A dependent's array is never used, so never read
        arr, problem = (None, None) if dependent else _read_array(record.word_list)
        out[record.note_id] = _Vocab(
            record=record,
            dependent=dependent,
            extraction_key=extraction_key,
            text_key=text_key,
            array=arr,
            array_problem=problem,
            key=array_key(arr) if arr is not None else text_key,
            forms=frozenset(
                form for form in (_norm(record.kanjified), _norm(record.normal)) if form
            ),
            reading=to_hiragana(_norm(record.reading)),
        )
    return out


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
    or not the origin is selected. `sentence_notes` is every note of the sentence type.
    `count_seen`: the sentence block names `sentence_seen_count_field`.

    `move_tags`, `copy_tags`: the sentence block's `migration_move_tags` and
    `migration_copy_tags`, the user's tags that go to the sentence note (`_UserTags.carry`).
    A listed tag also covers its `::` children, case aside, as a `tag:` search does. Raises
    TypeError when either is not a list of strings.
    """
    user_tags = _UserTags(
        _tag_list(move_tags, "migration_move_tags"), _tag_list(copy_tags, "migration_copy_tags")
    )
    report = Report()
    targets, by_key, existing_ids = _existing_targets(sentence_notes, report)
    vocab = _read_vocab(vocab_notes)
    migrated = {
        nid for nid, v in vocab.items() if _example_note_id(v.record.example_id) in existing_ids
    }
    selected_ids = sorted(set(selected))
    report.count("vocab notes read", len(vocab))
    report.count("sources", sum(1 for v in vocab.values() if not v.dependent))
    report.count("dependents", sum(1 for v in vocab.values() if v.dependent))
    report.count("selected", len(selected_ids))

    # Sources by sentence key. A source already given a sentence note is done: its array went
    # into that note when it was made, and merging it again would undo what the user changed
    # there since
    groups: dict[str, list[_Vocab]] = {}
    for v in vocab.values():
        if not v.dependent and v.id not in migrated and v.text_key:
            groups.setdefault(v.key, []).append(v)
    for key, sources in groups.items():
        target = by_key.get(key)
        if target is None:
            target = _Target(key=key, age_id=_origin(sources).id)
            targets.append(target)
            by_key[key] = target
        target.sources = sources
        _combine_sources(target)

    _assign(vocab, selected_ids, migrated, targets, by_key, report)
    for target in targets:
        for vocab_id in target.vocab_ids:
            _link_check(target, vocab[vocab_id], report)
    return _plan(targets, vocab, count_seen, user_tags, report)


def _existing_targets(
    sentence_notes: Iterable[SentenceRecord], report: Report
) -> tuple[list[_Target], dict[str, _Target], set[int]]:
    """The existing sentence notes keyed the same way as the sources, first, so that a group with
    the same sentence joins its note rather than making another; and the ids of all of them,
    those without text included (a vocab note naming one has been migrated all the same)."""
    targets: list[_Target] = []
    by_key: dict[str, _Target] = {}
    ids: set[int] = set()
    for record in sorted(sentence_notes, key=lambda r: r.note_id):
        ids.add(record.note_id)
        arr, _problem = _read_array(record.word_list)
        key = array_key(arr) if arr is not None else _text_key(record.fields)
        if not key:
            report.add("existing_no_text", record.note_id)
            continue
        target = _Target(
            key=key,
            age_id=record.note_id,
            existing=record,
            array=copy.deepcopy(arr),
            original=arr,
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
    selected: list[int],
    migrated: set[int],
    targets: list[_Target],
    by_key: dict[str, _Target],
    report: Report,
) -> None:
    """Give each selected vocab note its example. A target is made or changed only because a
    selected note is assigned to it, so a selection of sources alone makes every sentence note
    and leaves the dependents for a later run."""
    linking = _linking_targets(
        targets, {nid for nid in selected if nid in vocab and vocab[nid].dependent}
    )
    for nid in selected:
        v = vocab.get(nid)
        if v is None:
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
        target: Optional[_Target] = by_key[v.key] if not v.dependent else None
        if v.dependent:
            arr, problem = match_flags.read_word_array(v.record.word_list)
            if arr or problem is not None:
                report.add("dependent_array", nid)
            target = _dependent_target(v, linking.get(nid, []), by_key, report)
        if target is not None:
            target.vocab_ids.append(nid)


def _linking_targets(targets: list[_Target], dependents: set[int]) -> dict[int, list[_Target]]:
    """Per dependent, the sentences whose array (as combined) links it."""
    linking: dict[int, list[_Target]] = {}
    for target in targets:
        if target.array is None:
            continue
        for _, elem in match_flags.iter_words(target.array):
            nid = match_flags.matched_note_id(elem)
            if nid is not None and nid in dependents:
                found = linking.setdefault(nid, [])
                if target not in found:
                    found.append(target)
    return linking


def _dependent_target(
    v: _Vocab, linking: list[_Target], by_key: dict[str, _Target], report: Report
) -> Optional[_Target]:
    """The sentence linking the dependent; of several, the one with its text, else the oldest;
    of none, the one with its text."""
    if len(linking) > 1:
        same_text = [t for t in linking if t.key == v.text_key]
        if same_text:
            # The usual case: the match op links a word's note from every sentence it meets
            # the word in after, and the note's own text says which one it was made from
            report.count("dependents linked from several sentences, chosen by their text")
            return same_text[0]
        oldest = min(linking, key=lambda t: t.age_id)
        others = ", ".join(t.label for t in linking)
        report.add("dependent_several", v.id, f"linked from {others}; {oldest.label} taken")
        return oldest
    if linking:
        if linking[0].key != v.text_key:
            report.add("dependent_other_sentence", v.id, linking[0].label)
        return linking[0]
    by_text = by_key.get(v.text_key)
    if by_text is None:
        report.add("dependent_no_example", v.id)
    return by_text


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
    """Link V's word in its example's array when nothing there links V yet, by V's <b>."""
    arr = target.array
    if arr is None:
        report.add("link_no_array", v.id, target.label)
        return
    if any(match_flags.matched_note_id(elem) == v.id for _, elem in match_flags.iter_words(arr)):
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
    elem[4] = [v.id]
    report.add("linked", v.id, f"{elem[2]}[{elem[3]}] {how} ({target.label})")
    report.count("links set by the link check")


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _pick_element(
    entries: list[_Entry], span: tuple[int, int], v: _Vocab
) -> tuple[Optional[list], str, str]:
    """(the element, how it was found, ""), or (None, the report case, what else its line
    says). In order: an element at the <b> that is V's word; for a dependent, whose <b> may
    still mark its source's word (the match op copied the sentence as it was), the one element
    anywhere that is V's word; for a source, whose <b> marks its own word, the element at
    exactly the <b>, else the smallest around it, when it has V's form or reading."""
    words = [e for e in entries if v.is_word(e.elem)]
    at_bold = [e for e in words if e.span is not None and _overlaps(e.span, span)]
    # A word whose place is unknown may be at the <b> too
    unplaced = [e for e in words if e.span is None]
    if at_bold:
        if len(at_bold) == 1 and not unplaced:
            return at_bold[0].elem, "at its <b>", ""
        return None, "link_several", ""
    if v.dependent:
        if len(words) == 1:
            return words[0].elem, "the one element of its word", ""
        return None, "link_several" if words else "link_no_element", ""
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

    def carry(
        self, target: _Target, vocab: Mapping[int, _Vocab]
    ) -> tuple[list[str], dict[int, list[str]]]:
        """The tags the target's sentence note gets, as the oldest note carrying one spells it,
        and per vocab note the tags it loses.

        A tag moves from each selected note assigned to the target, sources and dependents
        alike: a dependent's was put there by hand, about the sentence its card showed. It is
        copied from the target's sources, selected or not, since the sentence is theirs, and
        never from a dependent, whose copy is about its own word. A source not selected loses
        nothing and gives no tag to move: the run changes no vocab note the user left out, and
        the run that selects it joins it to this sentence note and moves its tags then."""
        selected = set(target.vocab_ids)
        sources = {s.id for s in target.sources}
        added: list[str] = []
        removed: dict[int, list[str]] = {}
        for nid in sorted(selected | sources):
            moved_under: set[str] = set()
            copied_under: set[str] = set()
            for tag in vocab[nid].record.tags:
                under = [listed for listed in self.move if _falls_under(tag, listed)]
                if under:
                    if nid in selected:
                        _add_tags(added, [tag])
                        _add_tags(removed.setdefault(nid, []), [tag])
                        moved_under.update(under)
                    continue
                under = [listed for listed in self.copy if _falls_under(tag, listed)]
                if under and nid in sources:
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
    count_seen: bool,
    user_tags: _UserTags,
    report: Report,
) -> MigrationPlan:
    new_sentences: list[NewSentence] = []
    updates: list[SentenceUpdate] = []
    examples: dict[int, Example] = {}
    tags_to_remove: dict[int, list[str]] = {}
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
        carried, lost = user_tags.carry(target, vocab)
        _add_tags(tags, carried)
        for vocab_id, vocab_tags in lost.items():
            _add_tags(tags_to_remove.setdefault(vocab_id, []), vocab_tags)
        index = len(new_sentences)
        new_sentences.append(
            NewSentence(
                origin_id=origin.id,
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
        assert existing is not None
        word_list = None
        if target.array is not None and target.array != target.original:
            word_list = match_flags.format_word_array(target.array)
        tags_to_add: list[str] = []
        _add_tags(tags_to_add, [CONFLICT_TAG] if target.conflict else [], existing.tags)
        carried, lost = user_tags.carry(target, vocab)
        _add_tags(tags_to_add, carried, existing.tags)
        for vocab_id, vocab_tags in lost.items():
            _add_tags(tags_to_remove.setdefault(vocab_id, []), vocab_tags)
        updates.append(
            SentenceUpdate(
                note_id=existing.note_id,
                word_list=word_list,
                tags_to_add=tags_to_add,
                source_ids=[s.id for s in target.sources],
                vocab_ids=list(target.vocab_ids),
            )
        )
        for vocab_id in target.vocab_ids:
            examples[vocab_id] = Example(note_id=existing.note_id)

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
    for listed, notes in user_tags.moved_from.items():
        report.count(f'tag "{listed}" moved from vocab notes', notes)
    for listed, notes in user_tags.copied_from.items():
        report.count(f'tag "{listed}" copied from vocab notes', notes)
    return MigrationPlan(new_sentences, updates, examples, tags_to_remove, report)
