"""Respells `vocab-kanjified` to the spelling the word arrays link to, and redraws the furigana.

A word array element carries the word's dictionary form: `[raw_text, pos, dict_form, reading,
match_data, sub_words]`. Everything that looks a note up by its word — `match_words_to_notes`
single-word mode, `tag_notes_matched_status`, `word_array_query_regex`, and the copy_anywhere
definitions over `sentence-vocab-list` — keys on `(dict_form, reading)` and finds the note
through `vocab-kanjified` + `vocab-kana`. Where the two spellings disagree the lookup misses,
even though the link itself is stored and works.

`vocab-kanjified` is not "what kanjify_sentence would write in a sentence": kanjify asks how a
word is written in context doing grammar, and an array element stores both — `持[も]ってきた` as
`raw_text` and 持って来る as `dict_form`. This field tracks the second one, the word as a lexical
entry, which is why a て-helper is kanji here and kana in the sentence.

Since the link already says which element is the note's word, the spelling is read straight off
the array; nothing is re-derived. Only notes whose reading already agrees are respelled, so a
disagreement can never silently repoint a note at a different word. `vocab` is never touched —
it holds the common, often kana, form on purpose. A note with `ignore-kanjified-form` set is
skipped.

Each respelled note also gets `vocab-furigana` and `vocab-processed-furigana` redrawn from the
new spelling with `make_furigana_from_reading`, the same call `match_words_to_notes` uses for a
new note, and the result is checked to read back as the word and the reading before it is kept.
`vocab-kanji-grades` is left alone and goes stale; the applied notes are tagged
`word-array-respelled` so the "Vocab note <-- Kanji notes - kanji grades" definition can be
re-run over them.

    py -3.10 word_array/research/vocab_respell.py [--fetch]   # list the changes, write nothing
    py -3.10 word_array/research/vocab_respell.py --apply
    py -3.10 word_array/research/vocab_respell.py --revert

`--apply` writes only where the three fields are still what the dump says; each write appends the
old values to `output/vocab_respell_undo.jsonl` first, since Anki cannot undo `updateNoteFields`.
`--revert` puts them back and removes the tag.

Report `output/vocab_respell_report.txt`, changes `output/vocab_respell_changes.jsonl`.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple, Optional

from _bootstrap import ADDON_ROOT, load_shared

import anki_connect
import vocab_dupes

REPORT = ADDON_ROOT / "output" / "vocab_respell_report.txt"
CHANGES = ADDON_ROOT / "output" / "vocab_respell_changes.jsonl"
UNDO = ADDON_ROOT / "output" / "vocab_respell_undo.jsonl"
TAG = "word-array-respelled"

KANJIFIED_FIELD = "vocab-kanjified"
FURIGANA_FIELD = "vocab-furigana"
PROCESSED_FIELD = "vocab-processed-furigana"
IGNORE_FIELD = "ignore-kanjified-form"

TAG_RE = re.compile(r"<[^>]+>")
GROUP_RE = re.compile(r" ?([^ \[\]<>]+?)\[([^\]]*)\]")

_kana_modules: dict = {}


def _kana():
    """The furigana pair, imported late: loading them pulls in the mecab controller."""
    if not _kana_modules:
        _kana_modules["make"] = load_shared("jp_text_processing.kana.make_furigana_from_reading")
        _kana_modules["highlight"] = load_shared("jp_text_processing.kana.kana_highlight")
        _kana_modules["conv"] = load_shared("jp_text_processing.mecab_controller.kana_conv")
    return _kana_modules["make"], _kana_modules["highlight"]


# --- reading the arrays -------------------------------------------------------------------


def iter_words(array):
    """Every word element of a word array, sub-words included. Tags and punctuation, which are
    one-element lists, are not words."""
    for element in array:
        if isinstance(element, list) and len(element) >= 6:
            yield element
            yield from iter_words(element[5])


def linked_note_id(match_data) -> Optional[int]:
    """The note a `match_data` links to: `[note_id]` or `[note_id, quality]`. A negative id is a
    placeholder `match_words_to_notes` left for a note it has not added yet."""
    if not match_data or not isinstance(match_data[0], int):
        return None
    return match_data[0] if match_data[0] > 0 else None


def links_by_note(rows: list) -> dict:
    """Per linked note id, a Counter of the `(dict_form, reading)` the arrays link it by."""
    out: dict = defaultdict(Counter)
    for row in rows:
        field = (row.get("sentence-vocab-list") or "").strip()
        if not field.startswith("["):
            continue
        try:
            array = json.loads(field.replace("&nbsp;", " "))
        except json.JSONDecodeError:
            continue
        if not isinstance(array, list):
            continue
        for word in iter_words(array):
            nid = linked_note_id(word[4])
            if nid is not None:
                out[nid][(word[2], word[3])] += 1
    return out


# --- the furigana the new spelling needs --------------------------------------------------


def furigana_reads_back(furigana: str, word: str, reading: str) -> bool:
    """The generated furigana really is this word with this reading: dropping the readings gives
    the word back, and keeping them gives the reading back.

    The readings are compared as hiragana, since a word keeps its own kana as it writes them —
    スペイン語[すぺいんご] becomes `スペイン 語[ご]`, which reads back as スペインご.
    """
    _kana()
    to_hiragana = _kana_modules["conv"].to_hiragana
    plain = TAG_RE.sub("", furigana)
    without = GROUP_RE.sub(lambda m: m[1], plain).replace(" ", "")
    kana = GROUP_RE.sub(lambda m: m[2], plain).replace(" ", "")
    return without == word and to_hiragana(kana) == to_hiragana(reading)


def redraw_furigana(word: str, reading: str) -> tuple:
    """`(vocab-furigana, vocab-processed-furigana)` for the word, or `(None, why not)`."""
    make, highlight = _kana()
    try:
        furigana = make.make_furigana_from_reading(word, reading)
    except Exception as e:  # the kana pipeline raises a variety of its own errors
        return None, "%s: %s" % (type(e).__name__, e)
    if not furigana:
        return None, "no furigana generated"
    if not furigana_reads_back(furigana, word, reading):
        return None, "furigana %r does not read back as %s / %s" % (furigana, word, reading)
    try:
        processed = highlight.kana_highlight(
            kanji_to_highlight="",
            text=furigana,
            return_type="furigana",
            with_tags_def=highlight.WithTagsDef(
                with_tags=True,
                merge_consecutive=False,
                onyomi_to_katakana=False,
                include_suru_okuri=True,
            ),
        )
    except Exception as e:
        return None, "processed furigana: %s: %s" % (type(e).__name__, e)
    return (furigana, processed), ""


# --- deciding what to change --------------------------------------------------------------


class Change(NamedTuple):
    nid: int
    key: str
    before: str  # vocab-kanjified now
    after: str  # the spelling the arrays link by
    reading: str
    furigana_before: str
    furigana_after: str
    processed_before: str
    processed_after: str
    links: int


def plan(rows: list) -> tuple:
    """The respellings to make, and the note groups held back with the reason each."""
    # A dump taken before these two fields joined vocab_dupes.FIELDS would read them as empty,
    # and the undo file would then offer to restore an empty furigana.
    missing = [f for f in (PROCESSED_FIELD, IGNORE_FIELD) if rows and f not in rows[0]]
    if missing:
        raise anki_connect.AnkiConnectError(
            "The dump predates %s; re-run with --fetch." % ", ".join(missing)
        )
    links = links_by_note(rows)
    held: dict = defaultdict(list)
    changes = []

    for row in rows:
        nid = row["nid"]
        by_word = links.get(nid)
        if not by_word:
            continue
        kanjified = (row.get(KANJIFIED_FIELD) or "").strip()
        kana = (row.get("vocab-kana") or "").strip()
        vocab = (row.get("vocab") or "").strip()
        key = (row.get("vocab-key") or "").strip()
        note = "%d %s" % (nid, key or vocab)

        if (row.get(IGNORE_FIELD) or "").strip():
            if any(form != kanjified for form, _ in by_word):
                held["the note sets ignore-kanjified-form"].append(note)
            continue

        forms = {form for form, _ in by_word}
        readings = {reading for _, reading in by_word}
        if len(forms) > 1 or len(readings) > 1:
            spellings = ", ".join(
                "%s [%s] x%d" % (form, reading, n) for (form, reading), n in by_word.most_common()
            )
            held["the arrays link the note by more than one word"].append(
                "%s: has %s [%s], linked by %s" % (note, kanjified, kana, spellings)
            )
            continue

        form, reading = next(iter(by_word))
        if form == kanjified and reading == kana:
            continue
        if reading != kana:
            held[
                "the reading differs, so the spelling cannot be trusted to be the same word"
                if form != kanjified
                else "the spelling agrees but the reading differs"
            ].append("%s: has %s [%s], linked by %s [%s]" % (note, kanjified, kana, form, reading))
            continue
        if form == vocab:
            held[
                "the arrays link by the note's `vocab`, which already matches, and respelling "
                "would lose the kanjified form"
            ].append("%s: has %s / vocab %s, linked by %s" % (note, kanjified, vocab, form))
            continue
        if not kanjified:
            held["the note has no vocab-kanjified to replace"].append(note)
            continue

        drawn, why_not = redraw_furigana(form, kana)
        if drawn is None:
            held["the new spelling and the reading cannot be drawn as furigana"].append(
                "%s: %s [%s] -> %s: %s" % (note, kanjified, kana, form, why_not)
            )
            continue
        furigana, processed = drawn
        changes.append(
            Change(
                nid=nid,
                key=key,
                before=kanjified,
                after=form,
                reading=kana,
                furigana_before=(row.get(FURIGANA_FIELD) or "").strip(),
                furigana_after=furigana,
                processed_before=(row.get(PROCESSED_FIELD) or "").strip(),
                processed_after=processed,
                links=sum(by_word.values()),
            )
        )
    changes.sort(key=lambda c: (-c.links, c.after))
    return changes, held


def report(changes: list, held: dict) -> list:
    lines = [
        "%d notes to respell, covering %d array links."
        % (len(changes), sum(c.links for c in changes)),
        "",
        "vocab-kanjified is set to the word array's dict_form; vocab and vocab-kana are left",
        "alone, and vocab-furigana / vocab-processed-furigana are redrawn from the new spelling.",
        "vocab-kanji-grades goes stale: re-run the kanji grades definition over tag:%s." % TAG,
        "",
        "%-8s %-16s %-16s %-14s %s" % ("links", "now", "respelled to", "reading", "new furigana"),
    ]
    for c in changes:
        lines.append(
            "%-8d %-16s %-16s %-14s %s" % (c.links, c.before, c.after, c.reading, c.furigana_after)
        )

    lines += ["", "--- held back, nothing written for these ---"]
    for reason in sorted(held, key=lambda r: -len(held[r])):
        notes = held[reason]
        lines.append("")
        lines.append("%d: %s" % (len(notes), reason))
        for note in notes[:60]:
            lines.append("    %s" % note)
        if len(notes) > 60:
            lines.append("    ... and %d more" % (len(notes) - 60))
    return lines


# --- writing (over AnkiConnect) -----------------------------------------------------------


class Write(NamedTuple):
    nid: int
    before: dict
    after: dict


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _current(infos: list) -> dict:
    """Per note id, the three fields this script writes as they are in Anki now."""
    out = {}
    for info in infos:
        if not info:
            continue
        values = info.get("fields", {})
        out[info["noteId"]] = {
            name: (values.get(name, {}).get("value", "") or "").strip()
            for name in (KANJIFIED_FIELD, FURIGANA_FIELD, PROCESSED_FIELD)
        }
    return out


def plan_writes(changes: list, infos: list) -> tuple:
    """The writes for the notes still holding what the dump said; why the others are refused."""
    current = _current(infos)
    writes, refused = [], []
    for c in changes:
        now = current.get(c.nid)
        if now is None:
            refused.append("nid %d: no such note" % c.nid)
            continue
        before = {
            KANJIFIED_FIELD: c.before,
            FURIGANA_FIELD: c.furigana_before,
            PROCESSED_FIELD: c.processed_before,
        }
        after = {
            KANJIFIED_FIELD: c.after,
            FURIGANA_FIELD: c.furigana_after,
            PROCESSED_FIELD: c.processed_after,
        }
        if now[KANJIFIED_FIELD] not in (c.before, c.after):
            refused.append(
                "nid %d: vocab-kanjified is %r, the dump said %r"
                % (c.nid, now[KANJIFIED_FIELD], c.before)
            )
            continue
        writes.append(Write(c.nid, before, after))
    return writes, refused


def apply(client, changes: list, undo: Path) -> tuple:
    """Writes the fields and tags the notes, recording both in `undo` first; how many notes were
    written, how many tagged, and the refusals."""
    infos = client.notes_info([c.nid for c in changes]) if changes else []
    writes, refused = plan_writes(changes, infos)
    written = 0
    for write in writes:
        with open(undo, "a", encoding="utf-8") as out:
            out.write(json.dumps(write._asdict(), ensure_ascii=False) + "\n")
        if write.before != write.after:
            client.update_note_fields(write.nid, write.after)
            written += 1
    if writes:
        client.add_tags([w.nid for w in writes], TAG)
    return written, len(writes), refused


def revert(client, undo: Path) -> tuple:
    """Puts back the fields of `undo`, newest first, where they are still what was written, and
    removes the tag; reverted entries leave the file."""
    entries = [Write(**row) for row in read_jsonl(undo)]
    infos = client.notes_info(sorted({e.nid for e in entries})) if entries else []
    current = _current(infos)
    kept, refused, reverted = [], [], 0
    for entry in reversed(entries):
        now = current.get(entry.nid, {})
        if now.get(KANJIFIED_FIELD) != entry.after[KANJIFIED_FIELD]:
            refused.append("nid %d: vocab-kanjified changed since the respell, left as is" % entry.nid)
            kept.append(entry)
            continue
        client.update_note_fields(entry.nid, entry.before)
        current[entry.nid] = dict(entry.before)
        reverted += 1
        client.remove_tags([entry.nid], TAG)
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
            print("\n".join(refused + ["reverted %d notes, removed the tag" % reverted]))
            return 0
        if args.fetch:
            print("%d notes -> %s" % (vocab_dupes.fetch(), vocab_dupes.DUMP))
        changes, held = plan(vocab_dupes.read_dump())
        lines = report(changes, held)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with open(CHANGES, "w", encoding="utf-8") as out:
            for change in changes:
                out.write(json.dumps(change._asdict(), ensure_ascii=False) + "\n")
        print(lines[0])
        if not args.apply:
            print("report in %s, changes in %s" % (REPORT, CHANGES))
            print("check it, then rerun with --apply")
            return 0
        written, tagged, refused = apply(client, changes, args.undo)
        print("\n".join(refused))
        print("wrote %d notes, tagged %d, refused %d" % (written, tagged, len(refused)))
        print("old values in %s (--revert puts them back)" % args.undo)
        return 0
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
