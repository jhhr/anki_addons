"""Asks a model which of two readings of one spelling is the right one.

`vocab_reading_fix` repairs a damaged `vocab-kana` only where evidence settles it, which is 8
of 49 cases. What is left, together with the spellings that carry two real readings, is a
question about the word rather than about the data: 一段落 is いちだんらく or ひとだんらく,
狡賢い keeps its rendaku or does not, 醜女 read しこめ and read しゅうじょ are arguably not even
the same word. No rule reaches these, so each is put to a model, one word per request.

The model is given the spelling, both readings, the note's own meaning, and the sentences the
element actually appears in - with their furigana, which often shows the reading outright - and
answers with one of four verdicts:

- `array`   the note's reading is wrong, so `vocab-kana` is repaired to the array's.
- `note`    the note's reading is right, so the array element is the wrong one.
- `split`   one spelling, two different words. Neither reading is wrong and the element wants
            a note of its own, which is the judge op's call, not this one's.
- `unsure`  nothing is written.

Only `array` is written from here, through `vocab_reading_fix`'s own repair path, because that
is the verdict this script can act on without touching a word array. `note` and `split` are
reported for the array-side pass.

Responses are cached by model and prompt in `output/vocab_reading_judge_results.jsonl`, so a
rerun costs nothing for questions already asked.

    py -3.10 word_array/research/vocab_reading_judge.py            # list the questions
    py -3.10 word_array/research/vocab_reading_judge.py --ask      # ask the model (costs)
    py -3.10 word_array/research/vocab_reading_judge.py --apply    # write the `array` verdicts
    py -3.10 word_array/research/vocab_reading_judge.py --revert

Report `output/vocab_reading_judge_report.txt`.
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

from _bootstrap import ADDON_ROOT, load_shared

import anki_connect
import vocab_dupes
import vocab_morphology
import vocab_reading_fix
import vocab_respell
import vocab_unlink

to_hiragana = load_shared("jp_text_processing.mecab_controller.kana_conv").to_hiragana

RESULTS = ADDON_ROOT / "output" / "vocab_reading_judge_results.jsonl"
REPORT = ADDON_ROOT / "output" / "vocab_reading_judge_report.txt"
UNDO = ADDON_ROOT / "output" / "vocab_reading_judge_undo.jsonl"

MODEL = "claude-sonnet-5"
MAX_SENTENCES = 3
TAGS = re.compile(r"<[^>]+>")

# The families this asks about: one spelling, two readings, and no evidence that settles it.
JUDGEABLE = vocab_reading_fix.REPAIRABLE | {vocab_morphology.TWO_READINGS}

ARRAY, NOTE, SPLIT, UNSURE = "array", "note", "split", "unsure"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": [ARRAY, NOTE, SPLIT, UNSURE]},
        "reading": {
            "type": "string",
            "description": "The reading that is right, in hiragana. Empty for split or unsure.",
        },
        "why": {"type": "string", "description": "One sentence, in English."},
    },
    "required": ["verdict", "reading", "why"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You are settling how a Japanese vocabulary note should be read.

A note spells a word one way and reads it one way. A word array built from real sentences
spells it the same way but reads it differently. Exactly one of these is true:

- "array": the note's reading is a mistake and the array's reading is the word's real reading.
- "note": the note's reading is right and the array's reading is the mistake.
- "split": the spelling has two genuinely different words with different readings and
  different meanings, so neither reading is a mistake. If you are told a separate note already
  exists for the other reading, that is evidence for this.
- "unsure": you cannot tell.

Judge by the reading the word really has, not by which source looks more official. Rendaku
goes both ways: a compound may genuinely voice (三つ子 みつご) or genuinely not. The sentences
are the strongest evidence, and their furigana often gives the reading outright.

Answer for the one word you are given. Put the correct reading in hiragana in "reading" when
the verdict is "array" or "note", and leave it empty otherwise."""


class Case(NamedTuple):
    note_id: int
    key: str
    spelling: str
    note_reading: str  # as the note stores it
    array_reading: str  # as the element stores it
    meaning: str
    links: int
    sentences: tuple
    family: str
    owned_by: tuple  # other notes whose own word this reading already is


def clean(text: str) -> str:
    text = TAGS.sub(" ", (text or "").replace("<br>", " ").replace("&nbsp;", " "))
    return " ".join(text.split())


def collect(rows: list) -> list:
    """Every case where one spelling has two readings and no evidence decides between them."""
    if rows and vocab_unlink.ARRAY_FIELD not in rows[0]:
        raise anki_connect.AnkiConnectError(
            "The dump has no %s; re-run vocab_dupes with --fetch." % vocab_unlink.ARRAY_FIELD
        )
    totals, where = vocab_unlink.links_in(rows)
    owners = vocab_unlink.owners_by_word(rows)
    by_nid = {row["nid"]: row for row in rows}
    cases = []
    for note_id, by_link in totals.items():
        row = by_nid.get(note_id)
        if row is None:
            continue
        spelling = (row.get(vocab_reading_fix.KANJIFIED_FIELD) or "").strip()
        reading = (row.get(vocab_reading_fix.READING_FIELD) or "").strip()
        kana = to_hiragana(reading)
        for link, count in by_link.most_common():
            link_kana = to_hiragana(link.reading)
            family = vocab_morphology.family(spelling, kana, link.form, link_kana)
            if family not in JUDGEABLE:
                continue
            others = owners.get((link.form, link_kana), set()) - {note_id}
            if others and family in vocab_reading_fix.REPAIRABLE:
                # `vocab_unlink` unlinks these: the element is the other note's word, so it
                # says nothing about this note's reading. It does not unlink a spelling with
                # two real readings, though, so those are asked about even when owned.
                continue
            if vocab_reading_fix.decide(spelling, kana, link_kana)[0] is not None:
                continue  # evidence settles it; vocab_reading_fix repairs it
            sentences = []
            for sentence_id in sorted(where[note_id][link])[:MAX_SENTENCES]:
                text = clean((by_nid.get(sentence_id) or {}).get("sentence-kanjified-furigana"))
                if text:
                    sentences.append(text)
            cases.append(
                Case(
                    note_id=note_id,
                    key=(row.get("vocab-key") or "").strip(),
                    spelling=spelling,
                    note_reading=reading,
                    array_reading=link.reading,
                    meaning=clean(row.get("vocab-translation") or row.get("meaning-jp"))[:200],
                    links=count,
                    sentences=tuple(sentences),
                    family=family,
                    owned_by=tuple(sorted(others)),
                )
            )
    cases.sort(key=lambda c: (-c.links, c.note_id))
    return cases


def prompt(case: Case) -> str:
    lines = [
        "Word as the note spells it: %s" % case.spelling,
        "Reading the note stores:    %s" % case.note_reading,
        "Reading the sentences use:  %s" % case.array_reading,
    ]
    if case.meaning:
        lines.append("What the note says it means: %s" % case.meaning)
    lines.append("The sentences use it %d time(s)." % case.links)
    if case.owned_by:
        lines.append(
            "Note: a separate vocabulary note already exists for %s read %s."
            % (case.spelling, case.array_reading)
        )
    if case.sentences:
        lines.append("")
        lines.append("Sentences it appears in:")
        lines += ["  %s" % s for s in case.sentences]
    return "\n".join(lines)


def key_for(model: str, text: str) -> str:
    return hashlib.sha1(("%s\n%s" % (model, text)).encode("utf-8")).hexdigest()


def read_results() -> dict:
    if not RESULTS.exists():
        return {}
    out = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            out[entry["key"]] = entry
    return out


def load_ops():
    """`base_ops` with Anki stubbed and the add-on's real config, as `proper_noun_eval` does."""
    sys.path.insert(0, str(ADDON_ROOT / "test"))
    from addon_modules import load_ops_module, mw

    config = json.loads((ADDON_ROOT / "config.json").read_text(encoding="utf-8"))
    meta = ADDON_ROOT / "meta.json"
    if meta.exists():
        config.update(json.loads(meta.read_text(encoding="utf-8")).get("config", {}))
    mw.addonManager = SimpleNamespace(getConfig=lambda _name: config)
    return load_ops_module("base_ops")


def ask(cases: list, model: str, workers: int, effort: str = "") -> dict:
    """Ask for every case not already answered, appending each answer as it lands."""
    cached = read_results()
    todo = [c for c in cases if key_for(model, prompt(c)) not in cached]
    if not todo:
        return cached
    ops = load_ops()

    def one(case):
        text = prompt(case)
        return key_for(model, text), case, ops.get_response(
            model,
            text,
            instructions=INSTRUCTIONS,
            response_schema=RESPONSE_SCHEMA,
            effort=effort or None,
        )

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, case, response in pool.map(one, todo):
                if not response:
                    continue  # not cached, so a rerun asks again
                entry = {
                    "key": key,
                    "model": model,
                    "effort": effort,
                    "nid": case.note_id,
                    "spelling": case.spelling,
                    "response": response,
                }
                cached[key] = entry
                out.write(json.dumps(entry, ensure_ascii=False) + "\n")
                out.flush()
    return cached


def verdicts(cases: list, model: str, cached: dict) -> list:
    """`(case, verdict, reading, why)` per case, with `unsure` where nothing came back."""
    out = []
    for case in cases:
        entry = cached.get(key_for(model, prompt(case)))
        answer = (entry or {}).get("response") or {}
        verdict = answer.get("verdict") or UNSURE
        if verdict not in (ARRAY, NOTE, SPLIT, UNSURE):
            verdict = UNSURE
        out.append((case, verdict, (answer.get("reading") or "").strip(), answer.get("why") or ""))
    return out


def repairs(judged: list) -> tuple:
    """The `array` verdicts as `vocab_reading_fix.Fix`, and the ones that could not be drawn."""
    fixes, refused = [], []
    for case, verdict, reading, why in judged:
        if verdict != ARRAY:
            continue
        wanted = to_hiragana(reading) or to_hiragana(case.array_reading)
        if wanted == to_hiragana(case.note_reading):
            refused.append("%s: the model's reading is the one the note already has" % case.key)
            continue
        drawn, why_not = vocab_respell.redraw_furigana(case.spelling, wanted)
        if drawn is None:
            refused.append("%s: %s cannot be drawn as furigana: %s" % (case.key, wanted, why_not))
            continue
        furigana, processed = drawn
        fixes.append(
            vocab_reading_fix.Fix(
                note_id=case.note_id,
                key=case.key,
                spelling=case.spelling,
                was=case.note_reading,
                now=wanted,
                why=why,
                furigana=furigana,
                processed=processed,
                links=case.links,
            )
        )
    return fixes, refused


def report(judged: list, fixes: list, refused: list, model: str) -> list:
    tally = Counter(verdict for _, verdict, _, _ in judged)
    lines = [
        "%d words judged by %s: %s"
        % (
            len(judged),
            model,
            ", ".join("%s %d" % (v, n) for v, n in tally.most_common()) or "nothing asked yet",
        ),
        "",
        "%d of them are readings to repair; run --apply to write those." % len(fixes),
        "",
    ]
    for verdict in (ARRAY, NOTE, SPLIT, UNSURE):
        rows = [j for j in judged if j[1] == verdict]
        if not rows:
            continue
        lines += ["", "=== %s (%d) ===" % (verdict, len(rows)), ""]
        for case, _, reading, why in rows:
            lines.append(
                "%-16s note [%s] vs array [%s] x%d%s"
                % (
                    case.key,
                    case.note_reading,
                    case.array_reading,
                    case.links,
                    "  -> %s" % reading if reading else "",
                )
            )
            lines.append("        %s" % why)
    if refused:
        lines += ["", "--- verdicts that could not be written ---"]
        lines += ["    %s" % r for r in refused]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ask", action="store_true", help="ask the model (costs money)")
    parser.add_argument("--apply", action="store_true", help="write the `array` verdicts")
    parser.add_argument("--revert", action="store_true", help="put the old readings back")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--effort",
        default="",
        choices=["", "low", "medium", "high"],
        help="how hard the model should think; empty leaves thinking off",
    )
    parser.add_argument("--undo", type=Path, default=UNDO)
    parser.add_argument("--anki-connect", default=None)
    args = parser.parse_args()

    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    try:
        if args.revert:
            reverted, refused = vocab_reading_fix.revert(client, args.undo)
            print("\n".join(refused + ["reverted %d notes" % reverted]))
            return 0
        cases = collect(vocab_dupes.read_dump())
        cached = (
            ask(cases, args.model, args.workers, args.effort)
            if args.ask
            else read_results()
        )
        judged = verdicts(cases, args.model, cached)
        fixes, refused = repairs(judged)
        lines = report(judged, fixes, refused, args.model)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(lines[0])
        if not args.apply:
            print("report in %s" % REPORT)
            if not args.ask:
                print("nothing asked; rerun with --ask to put the questions to %s" % args.model)
            return 0
        written, write_refused = vocab_reading_fix.apply(client, fixes, args.undo)
        print("\n".join(write_refused + ["wrote %d notes, undo in %s" % (written, args.undo)]))
    except anki_connect.AnkiConnectError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
