"""Cleans up the vocab notes the word arrays link by more than one word.

`vocab_respell` holds a note back when the arrays link it by several different `(dict_form,
reading)` pairs, because it cannot then tell which one is the note's word. This script decides
those cases and repairs the arrays instead of the notes.

The note's own reading is the anchor. A note is only touched when some link both reads and
spells like the note (`vocab-kanjified`, `vocab` or the `vocab-key` base form), which is the
link that says "this element really is this note's word". Every other link is then one of two
things, told apart by whether any *other* vocab note owns that `(spelling, reading)`:

- a different word that got the wrong note - a colloquial reading the generator left unnormalized
  (人[にん] on the 人[ひと] note), an inflection of a related word (願う on the 願い note), a
  homophone the kana-only lookup in `match_words_to_notes` cannot tell apart (浴 for よく), or a
  phrase the proper noun op swallowed the word into (ゆりちゃん on the ちゃん note). The element
  goes back to `["match"]` for `match_words_to_notes` to match again, or to `[]` when the form
  merely contains the note's word, since whether such a phrase deserves a note at all is the
  judge's call. `match_targets.unlink_missing_notes` puts a word back the same way.

- the same word spelled another way, owned by nobody - 如何ぞ for どうぞ, 這入る for 入る. Here
  the link is right and the array is what disagrees, so the element's `dict_form` is rewritten
  to the note's spelling and the link is kept. `raw_text` is left alone: it is the word as the
  sentence writes it, not the lexical entry.

A note linked by two of its own spellings is held back, since which of them a word array
should use is a judgement call about spelling rather than about which word the element is.

When *no* link both spells and reads like the note there is no anchor to believe, but the links
can still be judged one at a time on what they are. `vocab_morphology` names how the two
readings differ, and only the families that mean "a different word" are acted on: a deverbal
noun against its verb (行き on the 行く note), an inflected form against its plain one (空いた on
空く), a phrase built around the word. A family that is a damaged note reading (会社[がいしゃ]
for かいしゃ) belongs to the reading repair, not here, and one that is still being decided by
hand - the adverbial く-forms, the する-compounds, the ずる / じる pairs, a spelling with two
real readings - is held back until that decision is made. Nothing is guessed: a difference no
family explains is held back too.

Taking a note's only link away leaves nothing linking it. That is usually right, since the
arrays really do not contain the word, but it is listed separately in the report so it can be
seen before anything is written.

    py -3.10 word_array/research/vocab_unlink.py [--fetch]   # list the edits, write nothing
    py -3.10 word_array/research/vocab_unlink.py --apply
    py -3.10 word_array/research/vocab_unlink.py --revert

`--apply` rewrites `sentence-vocab-list` on the sentence notes, and only where the elements it
plans to touch are still the ones the dump saw; each note's old field text is appended to
`output/vocab_unlink_undo.jsonl` first, since Anki cannot undo `updateNoteFields`. `--revert`
puts the field text back.

Report `output/vocab_unlink_report.txt`, edits `output/vocab_unlink_edits.jsonl`.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

from _bootstrap import ADDON_ROOT, load, load_shared

import anki_connect
import vocab_dupes
import vocab_morphology

match_flags = load("match_flags")
to_hiragana = load_shared("jp_text_processing.mecab_controller.kana_conv").to_hiragana

REPORT = ADDON_ROOT / "output" / "vocab_unlink_report.txt"
EDITS = ADDON_ROOT / "output" / "vocab_unlink_edits.jsonl"
UNDO = ADDON_ROOT / "output" / "vocab_unlink_undo.jsonl"

ARRAY_FIELD = "sentence-vocab-list"
KANJIFIED_FIELD = "vocab-kanjified"

KANJI_RE = re.compile(r"[一-龯々〆ヶ]")

UNLINK = "unlink"  # back to ["match"], for match_words_to_notes
UNJUDGE = "unjudge"  # back to [], for the judge
RESPELL = "respell"  # keep the link, write the note's spelling as the element's dict_form

# What each family of `vocab_morphology` means for a link with no anchor to judge it by.
UNLINK_FAMILIES = {
    vocab_morphology.DEVERBAL,
    vocab_morphology.VERB_OF_DEVERBAL,
    vocab_morphology.INFLECTED,
    vocab_morphology.PLAIN_OF_INFLECTED,
    vocab_morphology.WORD_INSIDE,
}
UNJUDGE_FAMILIES = {vocab_morphology.PHRASE_AROUND}
# Families whose action is a standing decision rather than a rule, held until it is made.
AWAITING_A_DECISION = {
    # Not a decision so much as nothing to decide, but it belongs here rather than falling
    # through: the checks below ask whether another note owns the link's word, and for a link
    # whose reading slot is damaged the answer is yes for the wrong reason. DVD[DVD] is "owned"
    # by a note that reads DVD as "DVD", so falling through would unlink the element from the
    # note that has it right and hand it to the matcher, which would give it to the damaged one.
    vocab_morphology.NOT_A_QUESTION: vocab_morphology.NOT_A_QUESTION,
    vocab_morphology.KU_ADVERB: "the adverbial く-forms are being decided one by one",
    vocab_morphology.SURU_COMPOUND: "the する-compounds are being decided one by one",
    vocab_morphology.ZURU_JIRU: "the ずる / じる pairs are being decided one by one",
    vocab_morphology.TWO_READINGS: "the spelling agrees and both readings are real",
}
# Families where one of the two readings is wrong without the family saying which, so the
# link is evidence about the reading rather than a word to unlink. The reading repair decides
# them, where it can find evidence; it cannot be assumed that the note is the damaged side.
A_DAMAGED_READING = {
    vocab_morphology.RENDAKU,
    vocab_morphology.TYPO,
    vocab_morphology.WHITESPACE,
    vocab_morphology.WIDTH,
}


# --- reading the arrays -------------------------------------------------------------------


class Link(NamedTuple):
    form: str
    reading: str


def note_spellings(row: dict) -> set:
    """The spellings a note answers to: the ones `match_words_to_notes` looks it up by."""
    return {
        spelling
        for spelling in (
            (row.get(KANJIFIED_FIELD) or "").strip(),
            (row.get("vocab") or "").strip(),
            vocab_dupes.base_form((row.get("vocab-key") or "").strip()),
        )
        if spelling
    }


def owners_by_word(rows: list) -> dict:
    """Per `(spelling, hiragana reading)`, the notes that own it. A link to a word another note
    owns is a mislink: the matcher would have found that note, so this one is not the word's."""
    owners: dict = defaultdict(set)
    for row in rows:
        reading = to_hiragana((row.get("vocab-kana") or "").strip())
        for spelling in note_spellings(row):
            owners[(spelling, reading)].add(row["nid"])
    return owners


def links_in(rows: list) -> tuple:
    """`{note id: {Link: total}}`, `{note id: {Link: {sentence id: count}}}` and
    `{note id: {Link: shallowest depth}}`, over every word element of every array, sub-words
    included.

    The depth is what tells a word the sentence holds on its own from a piece of a compound,
    and `vocab_morphology.family` needs it: rendaku inside a compound is not a disagreement
    about how the word is read alone. The *shallowest* occurrence is the one kept, so a link
    that appears as a free-standing word anywhere is still judged as one.
    """
    totals: dict = defaultdict(Counter)
    where: dict = defaultdict(lambda: defaultdict(Counter))
    depths: dict = defaultdict(dict)
    for row in rows:
        array = match_flags.decode_word_array(row.get(ARRAY_FIELD) or "")
        if not array:
            continue
        for depth, element in match_flags.iter_words(array):
            if len(element) < 6:
                continue
            note_id = match_flags.matched_note_id(element)
            if note_id is None or note_id < 0:
                continue
            link = Link(element[2], element[3])
            totals[note_id][link] += 1
            where[note_id][link][row["nid"]] += 1
            seen = depths[note_id].get(link)
            depths[note_id][link] = depth if seen is None else min(seen, depth)
    return totals, where, depths


# --- deciding what to do ------------------------------------------------------------------


class Edit(NamedTuple):
    note_id: int  # the vocab note the elements link
    key: str  # its vocab-key, for the report
    form: str  # the element's dict_form now
    reading: str  # the element's reading
    action: str
    spelling: str  # for RESPELL, the dict_form to write instead
    why: str
    links: int  # how many elements this covers
    sentences: dict  # {sentence note id: how many of its elements}


def decide(link: Link, anchor: str, kana: str, others: set) -> tuple:
    """`(action, why)` for a link that is not the note's own word."""
    if to_hiragana(link.reading) != kana:
        if others:
            # A word of its own, so the matcher can place it without the judge being asked again.
            return UNLINK, "reads %s, not %s, and is already note %d's word" % (
                link.reading,
                kana,
                min(others),
            )
        if anchor in link.form and len(link.form) > len(anchor):
            # 七番組寮 around 七, ゆりちゃん around ちゃん: the proper noun op made a phrase no
            # note holds, so whether it is a word worth a note at all is the judge's call.
            return UNJUDGE, "reads %s, not %s: a phrase around %s that no note owns" % (
                link.reading,
                kana,
                anchor,
            )
        return UNLINK, "reads %s, not %s" % (link.reading, kana)
    if others:
        return UNLINK, "%s [%s] is note %d's own word" % (link.form, link.reading, min(others))
    return RESPELL, "no note owns %s, so it is %s spelled another way" % (link.form, anchor)


def decide_without_anchor(row: dict, link: Link, others: set, depth: int = 0) -> tuple:
    """`(action, why)` for a link on a note no element both spells and reads like.

    `(None, why)` when the difference is not this script's to settle, and `why` is then the
    reason the note is held back.
    """
    name = vocab_morphology.family(
        (row.get(KANJIFIED_FIELD) or "").strip(),
        to_hiragana((row.get("vocab-kana") or "").strip()),
        link.form,
        to_hiragana(link.reading),
        depth,
    )
    if name in AWAITING_A_DECISION:
        return None, AWAITING_A_DECISION[name]
    if name in A_DAMAGED_READING and not others:
        # One of the two readings is wrong and this cannot tell which. Unlinking would throw
        # away the element that is the evidence either way, so the reading repair gets it.
        return None, name
    if others:
        # The same rule as with an anchor: the matcher would have found that note, so this one
        # is not the word's owner.
        return UNLINK, "%s [%s] is note %d's own word" % (link.form, link.reading, min(others))
    if name in UNJUDGE_FAMILIES:
        return UNJUDGE, "%s, and no note owns it" % name
    if name in UNLINK_FAMILIES:
        return UNLINK, name
    return None, "no family explains the two readings"


def orphaned_notes(edits: list, rows: list) -> list:
    """The notes every one of whose links is taken away, so nothing will link them after.

    A note losing links that read exactly like it is a different case from one losing links to
    another word: every element it had was its own word under another spelling, which makes it
    a duplicate for `vocab_dupes` to merge rather than a word the sentences never use. It is
    marked so, since unlinking leaves it dangling either way.
    """
    totals, _, _ = links_in(rows)
    by_nid = {row["nid"]: row for row in rows}
    losing: dict = Counter()
    reads_alike: dict = defaultdict(list)
    for edit in edits:
        if edit.action == RESPELL:
            continue
        losing[edit.note_id] += edit.links
        row = by_nid.get(edit.note_id) or {}
        kana = to_hiragana((row.get("vocab-kana") or "").strip())
        reads_alike[edit.note_id].append(to_hiragana(edit.reading) == kana)
    out = []
    for note_id, lost in losing.items():
        if lost < sum(totals[note_id].values()):
            continue
        row = by_nid.get(note_id) or {}
        out.append(
            "%d %s [%s]%s"
            % (
                note_id,
                (row.get("vocab-key") or "").strip(),
                (row.get("vocab-kana") or "").strip(),
                "  <- reads like every link it lost: likely a duplicate note"
                if all(reads_alike[note_id])
                else "",
            )
        )
    return sorted(out)


def plan(rows: list) -> tuple:
    """The edits to make, and the notes held back with the reason for each."""
    if rows and ARRAY_FIELD not in rows[0]:
        raise anki_connect.AnkiConnectError("The dump has no %s; re-run with --fetch." % ARRAY_FIELD)
    totals, where, depths = links_in(rows)
    owners = owners_by_word(rows)
    by_nid = {row["nid"]: row for row in rows}
    held: dict = defaultdict(list)
    edits = []

    for note_id, by_link in totals.items():
        row = by_nid.get(note_id)
        if row is None:
            continue
        kana = to_hiragana((row.get("vocab-kana") or "").strip())
        mine = note_spellings(row)
        key = (row.get("vocab-key") or "").strip()
        note = "%d %s: has %s [%s], linked by %s" % (
            note_id,
            key,
            (row.get(KANJIFIED_FIELD) or "").strip(),
            (row.get("vocab-kana") or "").strip(),
            ", ".join("%s [%s] x%d" % (w.form, w.reading, n) for w, n in by_link.most_common()),
        )

        anchors = {w.form for w in by_link if to_hiragana(w.reading) == kana and w.form in mine}
        if not anchors:
            # No element both spells and reads like the note, so there is no anchor to believe.
            # Each link is judged on its own by what kind of word it is.
            for link, count in by_link.most_common():
                others = owners.get((link.form, to_hiragana(link.reading)), set()) - {note_id}
                action, why = decide_without_anchor(
                    row, link, others, depths[note_id].get(link, 0)
                )
                if action is None:
                    held[why].append(note)
                    continue
                edits.append(
                    Edit(
                        note_id=note_id,
                        key=key,
                        form=link.form,
                        reading=link.reading,
                        action=action,
                        spelling="",
                        why=why,
                        links=count,
                        sentences=dict(where[note_id][link]),
                    )
                )
            continue
        if len(anchors) > 1:
            # The note's kanjified form and its `vocab` are both linked, e.g. 珈琲 and コーヒー.
            # Which of the note's own spellings a word array should use is a policy question.
            held["the note is linked by two of its own spellings"].append(note)
            continue
        anchor = next(iter(anchors))

        for link, count in by_link.most_common():
            if link.form == anchor and to_hiragana(link.reading) == kana:
                continue
            others = owners.get((link.form, to_hiragana(link.reading)), set()) - {note_id}
            action, why = decide(link, anchor, kana, others)
            edits.append(
                Edit(
                    note_id=note_id,
                    key=key,
                    form=link.form,
                    reading=link.reading,
                    action=action,
                    spelling=anchor if action == RESPELL else "",
                    why=why,
                    links=count,
                    sentences=dict(where[note_id][link]),
                )
            )
    edits.sort(key=lambda e: (-e.links, e.note_id))
    return edits, held


def report(edits: list, held: dict, orphans=()) -> list:
    links: Counter[str] = Counter()
    for edit in edits:
        links[edit.action] += edit.links
    sentences = {nid for edit in edits for nid in edit.sentences}
    lines = [
        "%d edits over %d word arrays, covering %d links on %d vocab notes."
        % (
            len(edits),
            len(sentences),
            sum(e.links for e in edits),
            len({e.note_id for e in edits}),
        ),
        "",
        "%s: the element goes back to [\"match\"] for match_words_to_notes  - %d links"
        % (UNLINK, links[UNLINK]),
        "%s: the element goes back to [] for the judge                    - %d links"
        % (UNJUDGE, links[UNJUDGE]),
        "%s: the link is kept and the element's dict_form rewritten       - %d links"
        % (RESPELL, links[RESPELL]),
        "",
        "%-9s %-7s %-16s %-14s %-16s %s"
        % ("action", "links", "linked by", "reading", "note", "why"),
    ]
    for edit in edits:
        lines.append(
            "%-9s %-7d %-16s %-14s %-16s %s"
            % (edit.action, edit.links, edit.form, edit.reading, edit.key, edit.why)
        )

    if orphans:
        lines += [
            "",
            "--- %d notes will have no array link left ---" % len(orphans),
            "The arrays hold no occurrence of these notes, which is what the unmatched-words",
            "query will now show. A note marked as a likely duplicate lost its links to another",
            "spelling of itself instead, and wants merging rather than rematching.",
        ]
        lines += ["    %s" % note for note in orphans[:60]]
        if len(orphans) > 60:
            lines.append("    ... and %d more" % (len(orphans) - 60))

    lines += ["", "--- held back, no array touched for these ---"]
    for reason in sorted(held, key=lambda r: -len(held[r])):
        notes = held[reason]
        lines += ["", "%d: %s" % (len(notes), reason)]
        lines += ["    %s" % note for note in notes[:60]]
        if len(notes) > 60:
            lines.append("    ... and %d more" % (len(notes) - 60))
    return lines


# --- writing (over AnkiConnect) -----------------------------------------------------------


def edits_by_sentence(edits: list) -> dict:
    """The edits regrouped the way they are written: one word array at a time."""
    out: dict = defaultdict(list)
    for edit in edits:
        for sentence_id in edit.sentences:
            out[sentence_id].append(edit)
    return out


def rewrite(array: list, edits: list, sentence_id: int) -> tuple:
    """Applies this sentence's edits to its decoded array in place; how many elements changed,
    and the edits whose elements are no longer there in the number the dump counted."""
    found: dict = defaultdict(list)
    for _, element in match_flags.iter_words(array):
        if len(element) < 6:
            continue
        note_id = match_flags.matched_note_id(element)
        if note_id is not None:
            found[(note_id, element[2], element[3])].append(element)

    # The elements are addressed by what they hold rather than by position, so an array that has
    # been regenerated since the dump is caught here instead of being edited in the wrong place.
    stale = [
        edit
        for edit in edits
        if len(found[(edit.note_id, edit.form, edit.reading)]) != edit.sentences[sentence_id]
    ]
    if stale:
        return 0, stale
    changed = 0
    for edit in edits:
        for element in found[(edit.note_id, edit.form, edit.reading)]:
            if edit.action == RESPELL:
                element[2] = edit.spelling
            else:
                element[4] = [] if edit.action == UNJUDGE else [match_flags.MATCH]
            changed += 1
    return changed, []


class Write(NamedTuple):
    nid: int
    before: str
    after: str


def plan_writes(edits: list, infos: list) -> tuple:
    """The field texts to write, and why any word array is refused."""
    grouped = edits_by_sentence(edits)
    writes, refused = [], []
    for info in infos:
        if not info:
            continue
        nid = info["noteId"]
        before = (info.get("fields", {}).get(ARRAY_FIELD, {}).get("value", "") or "")
        array = match_flags.decode_word_array(before)
        if not array:
            refused.append("nid %d: %s holds no word array now" % (nid, ARRAY_FIELD))
            continue
        changed, stale = rewrite(array, grouped[nid], nid)
        if stale:
            refused.append(
                "nid %d: %s no longer there as the dump saw them"
                % (nid, ", ".join("%s [%s]" % (e.form, e.reading) for e in stale))
            )
            continue
        if not changed:
            continue
        writes.append(Write(nid, before, match_flags.format_word_array(array)))
    return writes, refused


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def apply(client, edits: list, undo: Path) -> tuple:
    """Rewrites the arrays, recording each old field text in `undo` first; how many were written
    and the refusals."""
    grouped = edits_by_sentence(edits)
    infos = client.notes_info(sorted(grouped)) if grouped else []
    writes, refused = plan_writes(edits, infos)
    for write in writes:
        with open(undo, "a", encoding="utf-8") as out:
            out.write(json.dumps(write._asdict(), ensure_ascii=False) + "\n")
        client.update_note_fields(write.nid, {ARRAY_FIELD: write.after})
    return len(writes), refused


def revert(client, undo: Path) -> tuple:
    """Puts back the field texts of `undo`, newest first, where the array is still the one that
    was written; reverted entries leave the file."""
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
            refused.append("nid %d: the word array changed since the unlink, left as is" % entry.nid)
            kept.append(entry)
            continue
        client.update_note_fields(entry.nid, {ARRAY_FIELD: entry.before})
        now[entry.nid] = entry.before
        reverted += 1
    text = "".join(json.dumps(e._asdict(), ensure_ascii=False) + "\n" for e in reversed(kept))
    undo.write_text(text, encoding="utf-8")
    return reverted, refused


def main() -> int:
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
        rows = vocab_dupes.read_dump()
        edits, held = plan(rows)
        lines = report(edits, held, orphaned_notes(edits, rows))
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
        written, refused = apply(client, edits, args.undo)
        print("\n".join(refused))
        print("rewrote %d word arrays, refused %d" % (written, len(refused)))
        print("old field text in %s (--revert puts it back)" % args.undo)
        return 0
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
