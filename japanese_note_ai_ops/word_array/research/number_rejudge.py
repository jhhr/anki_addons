"""Reopens the numbers and counters that were flagged `dontmatch` before the rules changed.

`default_match_data` used to flag any number outside `numbers.MATCHED_NUMERALS`, and anything
holding such a number as a sub-word, before the judge was ever asked; the judge's noun rules then
flagged the numeral inside a counter expression and every date and length of time. Between them
they left 1584 elements `dontmatch`, 763 of which link a word the collection holds a note for.
The rules are fixed, but a stored `dontmatch` stays put, so those elements need reopening.

Three things can be said about such an element, and only the first needs no judgement:

- a note exists for its exact `(dict_form, reading)`. The collection already says it is a word,
  so the element goes back to `["match"]` and `match_words_to_notes` links it to the note that is
  already there. No judge call, and no new note can come of it. 一[いち], 三[さん], 十日[とおか].
- the new `default_match_data` would still flag it: a number written out of nothing but numerals
  and not a word of its own (二十八, 三十, 千九百三十五). Left alone.
- neither. The element goes back to `[]` so the next judge run decides it under the new rules,
  which may end in a new note - that is the point. 一年, 三月, 幾[いく].

Two kinds are held back from that last group, because a rule still in force says `dontmatch` and
sending them to the judge would only invite a note for a word that is not one:

- the counter つ, which the counter rules name: "dontmatch the counter つ (三つ): the number word
  keeps the note".
- a numeral written with the counting reading it only has inside a counter word - 三[みっ] of
  三つ, 二[ふた] of 二人, 一[いっ] of 一分 - told from `numbers.number_reading`, which gives the
  reading the numeral has on its own. One of these that does have a note is matched like any
  other: 一[ひと] is a word, and the note says so.

    py -3.10 word_array/research/number_rejudge.py [--fetch]   # list the edits, write nothing
    py -3.10 word_array/research/number_rejudge.py --apply
    py -3.10 word_array/research/number_rejudge.py --revert

`--apply` rewrites `sentence-vocab-list` on the sentence notes, and only where the elements it
plans to touch are still the ones the dump saw; each note's old field text is appended to
`output/number_rejudge_undo.jsonl` first, since Anki cannot undo `updateNoteFields`. `--revert`
puts the field text back.

Report `output/number_rejudge_report.txt`, edits `output/number_rejudge_edits.jsonl`.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple, Optional

from _bootstrap import ADDON_ROOT, load, load_shared

import anki_connect
import vocab_dupes
from vocab_unlink import owners_by_word

match_flags = load("match_flags")
numbers = load("numbers")
to_hiragana = load_shared("jp_text_processing.mecab_controller.kana_conv").to_hiragana

REPORT = ADDON_ROOT / "output" / "number_rejudge_report.txt"
EDITS = ADDON_ROOT / "output" / "number_rejudge_edits.jsonl"
UNDO = ADDON_ROOT / "output" / "number_rejudge_undo.jsonl"
# Every rewritten array, since every one of them has something for match_words_to_notes.
# The judge only ever looks at unjudged words, so running it over the same tag asks
# nothing about the arrays whose elements all went straight back to ["match"].
TAG = "word-array-rejudge"

ARRAY_FIELD = "sentence-vocab-list"

MATCH = "match"  # a note exists: back to ["match"] for match_words_to_notes
UNJUDGE = "unjudge"  # back to [] for the judge, under the new rules

COUNTING_COUNTER = "つ"  # つ


# --- reading the arrays -------------------------------------------------------------------


class Word(NamedTuple):
    pos: str
    form: str
    reading: str


def counts_something(element: list) -> bool:
    """Whether the element is a number or a counter, or is built on one. These are the words the
    old rules took out of matching, between the numerals rule and the judge's noun rules."""
    if element[1] in ("number", "counter"):
        return True
    return any(sub[1] == "number" for sub in element[5] if len(sub) > 1)


def flagged_words(rows: list) -> tuple:
    """`{Word: total}` and `{Word: {sentence id: count}}` over every `dontmatch` element that
    counts something."""
    totals: Counter = Counter()
    where: dict = defaultdict(Counter)
    for row in rows:
        array = match_flags.decode_word_array(row.get(ARRAY_FIELD) or "")
        if not array:
            continue
        for _, element in match_flags.iter_words(array):
            if len(element) < 6 or not counts_something(element):
                continue
            try:
                if match_flags.match_state(element) is not match_flags.MatchState.DONT_MATCH:
                    continue
            except ValueError:
                continue
            word = Word(element[1], element[2], element[3])
            totals[word] += 1
            where[word][row["nid"]] += 1
    return totals, where


def standard_reading(form: str) -> Optional[str]:
    """The reading the numeral has standing on its own, or None when the form is no numeral."""
    value = numbers.parse_number(form)
    if value is None:
        return None
    try:
        return numbers.number_reading(value)
    except (ValueError, IndexError, KeyError):
        return None


# --- deciding what to do ------------------------------------------------------------------


class Edit(NamedTuple):
    pos: str
    form: str
    reading: str
    action: str
    why: str
    links: int
    sentences: dict


def decide(word: Word, has_note: bool) -> tuple:
    """`(action, why, detail)` for a flagged word; a `None` action leaves it flagged, and
    `why` groups the report while `detail` says what is particular to this word."""
    if has_note:
        return MATCH, "a note for the word is already there", ""
    if word.pos == "number" and numbers.is_plain_numeral(word.form):
        if not numbers.is_matched_numeral(word.form):
            return None, "still a number written out of numerals and not a word of its own", ""
        standard = standard_reading(word.form)
        if standard and to_hiragana(word.reading) != to_hiragana(standard):
            return (
                None,
                "a counting reading the numeral only has inside a counter word",
                "on its own %s is %s" % (word.form, standard),
            )
    if word.pos == "counter" and word.form == COUNTING_COUNTER:
        return None, "the counter rules still say to dontmatch つ", ""
    return UNJUDGE, "no note and no rule against it: the judge decides under the new rules", ""


def plan(rows: list) -> tuple:
    """The edits to make, and the words left flagged with the reason for each."""
    if rows and ARRAY_FIELD not in rows[0]:
        raise anki_connect.AnkiConnectError("The dump has no %s; re-run with --fetch." % ARRAY_FIELD)
    totals, where = flagged_words(rows)
    owners = owners_by_word(rows)
    edits, held = [], defaultdict(list)

    for word, count in totals.most_common():
        has_note = bool(owners.get((word.form, to_hiragana(word.reading))))
        action, why, detail = decide(word, has_note)
        if action is None:
            held[why].append(
                "%s [%s] (%s) x%d%s"
                % (word.form, word.reading, word.pos, count, " - %s" % detail if detail else "")
            )
            continue
        edits.append(
            Edit(
                pos=word.pos,
                form=word.form,
                reading=word.reading,
                action=action,
                why=why,
                links=count,
                sentences=dict(where[word]),
            )
        )
    edits.sort(key=lambda e: (e.action, -e.links, e.form))
    return edits, held


def report(edits: list, held: dict) -> list:
    links = Counter()
    for edit in edits:
        links[edit.action] += edit.links
    sentences = {nid for edit in edits for nid in edit.sentences}
    for_the_judge = notes_for_the_judge(edits)
    lines = [
        "%d words over %d word arrays, covering %d flagged elements."
        % (len(edits), len(sentences), sum(e.links for e in edits)),
        "",
        "Every rewritten array is tagged %s. Run match_words_to_notes over that tag: all %d"
        % (TAG, len(sentences)),
        "have a word waiting for it. Run the judge over the same tag as well - it has",
        "something to decide on %d of them, and nothing to ask about the other %d."
        % (len(for_the_judge), len(sentences) - len(for_the_judge)),
        "",
        "%s:   back to [\"match\"], linked by match_words_to_notes to the note already there"
        "  - %d elements" % (MATCH, links[MATCH]),
        "%s: back to [], for the next judge run to decide under the new rules"
        "        - %d elements" % (UNJUDGE, links[UNJUDGE]),
        "",
        "%-9s %-8s %-16s %-14s %-11s %s"
        % ("action", "elements", "word", "reading", "pos", "why"),
    ]
    for edit in edits:
        lines.append(
            "%-9s %-8d %-16s %-14s %-11s %s"
            % (edit.action, edit.links, edit.form, edit.reading, edit.pos, edit.why)
        )

    lines += ["", "--- left flagged, no array touched for these ---"]
    for why in sorted(held, key=lambda r: -len(held[r])):
        words = held[why]
        lines += ["", "%d: %s" % (len(words), why)]
        lines += ["    %s" % word for word in words[:60]]
        if len(words) > 60:
            lines.append("    ... and %d more" % (len(words) - 60))
    return lines


# --- writing (over AnkiConnect) -----------------------------------------------------------


def edits_by_sentence(edits: list) -> dict:
    out: dict = defaultdict(list)
    for edit in edits:
        for sentence_id in edit.sentences:
            out[sentence_id].append(edit)
    return out


def rewrite(array: list, edits: list, sentence_id: int) -> tuple:
    """Applies this sentence's edits to its decoded array in place; how many elements changed,
    and the edits whose elements are no longer flagged in the number the dump counted."""
    found: dict = defaultdict(list)
    for _, element in match_flags.iter_words(array):
        if len(element) < 6:
            continue
        try:
            if match_flags.match_state(element) is not match_flags.MatchState.DONT_MATCH:
                continue
        except ValueError:
            continue
        found[(element[1], element[2], element[3])].append(element)

    # Addressed by what they hold rather than by position, so an array regenerated since the dump
    # is refused instead of edited in the wrong place.
    stale = [
        edit
        for edit in edits
        if len(found[(edit.pos, edit.form, edit.reading)]) != edit.sentences[sentence_id]
    ]
    if stale:
        return 0, stale
    changed = 0
    for edit in edits:
        for element in found[(edit.pos, edit.form, edit.reading)]:
            element[4] = [match_flags.MATCH] if edit.action == MATCH else []
            changed += 1
    return changed, []


class Write(NamedTuple):
    nid: int
    before: str
    after: str


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def plan_writes(edits: list, infos: list) -> tuple:
    grouped = edits_by_sentence(edits)
    writes, refused = [], []
    for info in infos:
        if not info:
            continue
        nid = info["noteId"]
        before = info.get("fields", {}).get(ARRAY_FIELD, {}).get("value", "") or ""
        array = match_flags.decode_word_array(before)
        if not array:
            refused.append("nid %d: %s holds no word array now" % (nid, ARRAY_FIELD))
            continue
        changed, stale = rewrite(array, grouped[nid], nid)
        if stale:
            refused.append(
                "nid %d: %s no longer flagged as the dump saw them"
                % (nid, ", ".join("%s [%s]" % (e.form, e.reading) for e in stale))
            )
            continue
        if not changed:
            continue
        writes.append(Write(nid, before, match_flags.format_word_array(array)))
    return writes, refused


def notes_for_the_judge(edits: list) -> list:
    """The sentence notes with an element going back to `[]`. Only these need a judge run:
    a note whose flagged elements all found a note of their own is done as soon as
    match_words_to_notes next runs, with nothing asked of the judge."""
    return sorted({nid for edit in edits if edit.action == UNJUDGE for nid in edit.sentences})


def apply(client, edits: list, undo: Path) -> tuple:
    grouped = edits_by_sentence(edits)
    infos = client.notes_info(sorted(grouped)) if grouped else []
    writes, refused = plan_writes(edits, infos)
    for write in writes:
        with open(undo, "a", encoding="utf-8") as out:
            out.write(json.dumps(write._asdict(), ensure_ascii=False) + "\n")
        client.update_note_fields(write.nid, {ARRAY_FIELD: write.after})
    tagged = sorted({write.nid for write in writes})
    if tagged:
        client.add_tags(tagged, TAG)
    for_the_judge = set(notes_for_the_judge(edits)) & set(tagged)
    return len(writes), len(for_the_judge), refused


def revert(client, undo: Path) -> tuple:
    entries = [Write(**row) for row in read_jsonl(undo)]
    infos = client.notes_info(sorted({e.nid for e in entries})) if entries else []
    now = {
        info["noteId"]: (info.get("fields", {}).get(ARRAY_FIELD, {}).get("value", "") or "")
        for info in infos
        if info
    }
    kept, refused, reverted = [], [], 0
    for entry in reversed(entries):
        if now.get(entry.nid) != entry.after:
            refused.append("nid %d: the word array changed since the rejudge, left as is" % entry.nid)
            kept.append(entry)
            continue
        client.update_note_fields(entry.nid, {ARRAY_FIELD: entry.before})
        client.remove_tags([entry.nid], TAG)
        now[entry.nid] = entry.before
        reverted += 1
    text = "".join(json.dumps(e._asdict(), ensure_ascii=False) + "\n" for e in reversed(kept))
    undo.write_text(text, encoding="utf-8")
    return reverted, refused


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="re-dump the notes over AnkiConnect")
    parser.add_argument("--undo", type=Path, default=UNDO)
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", action="store_true")
    args = parser.parse_args()

    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    try:
        if args.revert:
            reverted, refused = revert(client, args.undo)
            print("\n".join(refused + ["reverted %d word arrays" % reverted]))
            return 0
        if args.fetch:
            print("%d notes -> %s" % (vocab_dupes.fetch(), vocab_dupes.DUMP))
        edits, held = plan(vocab_dupes.read_dump())
        lines = report(edits, held)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with open(EDITS, "w", encoding="utf-8") as out:
            for edit in edits:
                out.write(json.dumps(edit._asdict(), ensure_ascii=False) + "\n")
        print(lines[0])
        if not args.apply:
            print("report in %s, edits in %s" % (REPORT, EDITS))
            print("check it, then rerun with --apply")
            return 0
        written, tagged, refused = apply(client, edits, args.undo)
        print("\n".join(refused))
        print("rewrote %d word arrays, refused %d" % (written, len(refused)))
        print("tagged them %s; run the matcher over tag:%s, and the judge too - it has" % (TAG, TAG))
        print("something to decide on %d of them and nothing to ask about the rest" % tagged)
        print("old field text in %s (--revert puts it back)" % args.undo)
        return 0
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
