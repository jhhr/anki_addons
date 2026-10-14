"""Takes the notes off the number-and-counter words the judge no longer matches.

The judge used to be told to match "a number plus a counter", so the arrays link 一階, 二回 and
三人 to notes, and `match_words_to_notes` added a note for every one that had none. Its rules
now say `dontmatch` for such a word unless it is a native number word (三つ, 二人, 十日) or has
come to mean more than the count (一番, 一杯). But a stored judgement stays put: the judge's
default mode only asks about unjudged words, and "Re-judge matched words" asks about every
matched word of a note, which is far more than these.

So this script asks about exactly these words. Every word made of a number and a counter
(`judge.rule_group`) that an array holds judged worth a note - `["match"]`, `[id]` or
`[id, quality]` - is put to the judge in its own sentence, through the op's request code and
model. Nothing is decided by the word alone: 一杯 is a glass in one sentence and "a lot" in the
next, and which it is here is the judge's call.

The generator gives such a word its two sub-words only where the furigana reads them apart
(九[きゅう] 人[にん]); read as one (九人[きゅうにん]) it comes out whole. So a whole word is
asked about too, under the rules of its own group, when some array holds the same word and
reading made of a number and a counter. Only then: 一緒, 一生 and every other word that merely
begins with a numeral are no business of this script.

- `dontmatch`: the element becomes `["dontmatch"]`. Its number and its counter are left as they
  are; they are words of their own and keep their notes.
- `match`: the element is left alone.

One element is never rewritten: a note's own word in its own array, the link from a note to
itself. If the note stays it is that word's note, whatever the judge makes of the sentence it
came with (the deck's note for 一種 has 一種独特 for its example), and if the note is deleted
its array goes with it. The answer still counts for the deletion: a note is not kept by its
own sentence unless the judge keeps the word there.

A note is deleted when all of this holds, and only then:

- `match_words_to_notes` added it: it carries the tag that op puts on every note it adds. A
  note that came with a deck is never deleted, whatever links it loses.
- it was never studied. Review history cannot be written back, so a note with a card past new
  is listed for a person to decide on.
- a link taken away here pointed at it.
- nothing links it any more. A link that stays keeps it, whether a sentence where the judge
  kept the word or another word altogether, and so does one from its own array. A link from
  the array of a note that is itself deleted here does not: such a note was copied from the
  sentence it was made for, array and all, so two of them can link each other.

The rest that lost a link are listed with the reason they stay.

    py -3.10 word_array/research/number_counter_unlink.py [--fetch]   # list, write nothing
    py -3.10 word_array/research/number_counter_unlink.py --ask       # send what is unasked
    py -3.10 word_array/research/number_counter_unlink.py --apply
    py -3.10 word_array/research/number_counter_unlink.py --revert

Without `--ask` no request is sent: the words without an answer are counted and left alone, so
is every note they link. Answers are kept by model and prompt in
`output/number_counter_unlink_answers.jsonl`, and a rerun only pays for prompts that changed.

`--apply` reads each note again and rewrites `sentence-vocab-list` from what Anki holds now, so
an array that changed since the dump is judged by what it says today and a word with no answer
is not touched; each note's old field text is appended to
`output/number_counter_unlink_undo.jsonl` first, since Anki cannot undo `updateNoteFields`.
Before a note is deleted Anki is asked again whether it still has the tag, has been studied
since, or is still named in any array, and what the note held is appended to
`output/number_counter_unlink_deleted.jsonl`: a deleted note cannot be written back under its
id. `--revert` puts the arrays back and no note: a word whose note is
gone is put back to `["match"]` by the next `match_words_to_notes` run
(`match_targets.unlink_missing_notes`), which then makes the note again.

    py -3.10 word_array/research/number_counter_unlink.py --restore [--ask]   # list
    py -3.10 word_array/research/number_counter_unlink.py --restore --apply

`--restore` is for a change of the judge's rules after a run, which has happened twice: 一杯
was to keep its note as a glass too, then every count with a second meaning was. It asks the
judge again, under the rules as they are now, about each element the undo file says a run
turned to `["dontmatch"]` and that still is one, and gives back the match_data it had where
the answer is now `match`. An element whose note the run deleted goes to `["match"]`
instead, for `match_words_to_notes` to make the note again. The undo file is kept in step, so
`--revert` still works on what is left. Report
`output/number_counter_unlink_restore_report.txt`.

Report `output/number_counter_unlink_report.txt`.
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, Optional

from _bootstrap import ADDON_ROOT, load

import anki_connect
import vocab_dupes
from judge_eval import load_judge, prompt_key

judge = load("judge")
match_flags = load("match_flags")

REPORT = ADDON_ROOT / "output" / "number_counter_unlink_report.txt"
RESTORE_REPORT = ADDON_ROOT / "output" / "number_counter_unlink_restore_report.txt"
ANSWERS = ADDON_ROOT / "output" / "number_counter_unlink_answers.jsonl"
UNDO = ADDON_ROOT / "output" / "number_counter_unlink_undo.jsonl"
DELETED = ADDON_ROOT / "output" / "number_counter_unlink_deleted.jsonl"

ARRAY_FIELD = "sentence-vocab-list"
# match_words_to_notes puts it on every note it adds, and nothing takes it off
ADDED_TAG = "new_matched_jp_word"
JUDGED_WORTH_A_NOTE = (
    match_flags.MatchState.MATCH,
    match_flags.MatchState.LINKED,
    match_flags.MatchState.RATED,
)

NOT_ADDED = "not added by match_words_to_notes: a note that came with a deck stays"
STUDIED = "studied: its review history could not be written back, so it is yours to delete"
STILL_LINKED = "still linked, by a sentence where the judge keeps the word or by another word"
NO_SUCH_NOTE = "no vocab note has this id"


# --- reading the arrays -------------------------------------------------------------------


class Word(NamedTuple):
    """A number-and-counter word judged worth a note, where it stands in an array."""

    nid: int  # the note whose array holds it
    elem: list
    prompt: str  # the judge's prompt about it, which is also what names its answer
    sentence: str
    linked: Optional[int]


def _walk(array: list):
    """(element, the words it is a component of, its sentence) for every word of an array."""
    stack: list = []
    for depth, elem, sentence in match_flags.iter_highlighted(array):
        del stack[depth:]
        parents = list(stack)
        stack.append(elem)
        yield elem, parents, sentence


def split_words(arrays: dict) -> set:
    """`(dict_form, reading)` of every word some array holds made of a number and a counter,
    whatever its judgement: what tells a whole 九人 from a whole 一緒."""
    return {
        (elem[2], elem[3])
        for array in arrays.values()
        for elem, parents, _ in _walk(array)
        if judge.rule_group(elem, parents) == judge.NUMBER_COUNTER_GROUP
    }


def counted_words(nid: int, array: list, split: frozenset = frozenset()) -> list:
    """The number-and-counter words of a decoded array that are judged worth a note: those
    made of the two, and those with no sub-words whose word and reading are in `split`."""
    out = []
    for elem, parents, sentence in _walk(array):
        group = judge.rule_group(elem, parents)
        if group != judge.NUMBER_COUNTER_GROUP:
            whole = not any(len(sub) > 1 for sub in elem[5])
            if not whole or (elem[2], elem[3]) not in split:
                continue
        try:
            if match_flags.match_state(elem) not in JUDGED_WORTH_A_NOTE:
                continue
        except ValueError:
            continue
        prompt = judge.word_prompt(**judge.word_inputs(elem, sentence, parents, group))
        out.append(Word(nid, elem, prompt, sentence, match_flags.matched_note_id(elem)))
    return out


def read_arrays(rows: list) -> dict:
    """`{note id: decoded array}` for every row whose field holds one."""
    arrays = {}
    for row in rows:
        text = row.get(ARRAY_FIELD) or ""
        # decode_word_array logs every field it cannot read; an empty one is no finding
        if text.strip():
            array = match_flags.decode_word_array(text)
            if array:
                arrays[row["nid"]] = array
    return arrays


# --- the judge's answers ------------------------------------------------------------------


def read_answers(path: Path) -> dict:
    """`{prompt key: decision}` of every answer kept so far."""
    answers = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                answer = (row.get("response") or {}).get(judge.DECISION_FIELD)
                if answer in (match_flags.MATCH, match_flags.DONT_MATCH):
                    answers[row["key"]] = answer
    return answers


def decision(word: Word, answers: dict, model: str) -> Optional[str]:
    return answers.get(prompt_key(model, word.prompt))


def ask(prompts: list, model: str, workers: int, path: Path) -> tuple:
    """Sends the prompts through the op's own request code and appends each answer to `path`
    as it arrives; how many were answered and how many failed. A failed one is not kept, so
    the next run asks again."""
    op, _, rules, _ = load_judge()

    def one(prompt: str):
        return prompt, op.get_response(model, prompt, response_schema=rules.RESPONSE_SCHEMA)

    answered = failed = 0
    started = time.monotonic()
    path.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as pool, path.open("a", encoding="utf-8") as out:
        for done, (prompt, response) in enumerate(pool.map(one, prompts), 1):
            if response is None:
                failed += 1
                continue
            row = {"key": prompt_key(model, prompt), "model": model, "response": response}
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            answered += 1
            if done % 50 == 0:
                minutes = (time.monotonic() - started) / 60
                print("  ...%d/%d, %.1f min" % (done, len(prompts), minutes), file=sys.stderr)
    return answered, failed


# --- deciding what to do ------------------------------------------------------------------


def own_word(word: Word) -> bool:
    """Whether the word is its note's own, linked to the note whose array holds it."""
    return word.linked == word.nid


class Plan(NamedTuple):
    unlink: list  # Words the judge says dontmatch about, to rewrite
    own: list  # the same answer about a note's own word, which is not rewritten
    kept: list  # Words the judge still matches
    unasked: list  # Words with no answer yet
    delete: list  # note ids, added for a word that nothing links any more
    held: dict  # why the other notes that lose a link stay: {reason: [note id]}
    split: frozenset  # split_words() of the dump, which the rewrite reads the live arrays by


def notes_to_delete(rows: list, arrays: dict, rejected: list) -> tuple:
    """The notes to delete and `{reason: [note id]}` for the others that lose a link, given
    every word the judge said dontmatch about, the notes' own words included."""
    by_nid = {row["nid"]: row for row in rows}
    gone = {id(word.elem) for word in rejected}
    lost = {word.linked for word in rejected if word.linked is not None}

    # Who still links whom once the unlinked words are taken out
    linked_from: dict = defaultdict(set)
    for nid, array in arrays.items():
        for _, elem in match_flags.iter_words(array):
            target = match_flags.matched_note_id(elem)
            if target in lost and id(elem) not in gone:
                linked_from[target].add(nid)

    held: dict = defaultdict(list)
    delete, studied = set(), set()
    for target in sorted(lost):
        if target not in by_nid:
            held[NO_SUCH_NOTE].append(target)
        elif ADDED_TAG not in by_nid[target].get("tags", []):
            held[NOT_ADDED].append(target)
        elif target in linked_from[target]:
            held[STILL_LINKED].append(target)
        elif by_nid[target].get("studied"):
            # Never in the set below: a note that stays keeps what its own array links
            studied.add(target)
        else:
            delete.add(target)
    # A link from a note that is deleted too holds nothing: drop from the set whatever a
    # note outside it links, until none is left
    while True:
        alive = {target for target in delete if linked_from[target] - delete}
        if not alive:
            break
        delete -= alive
        held[STILL_LINKED].extend(sorted(alive))
    for target in studied:
        held[STILL_LINKED if linked_from[target] - delete else STUDIED].append(target)
    # A note that stays for another reason and whose own word is all the judge turned down
    # loses nothing, as its own word is not rewritten: not worth listing
    losing = {word.linked for word in rejected if not own_word(word)}
    listed = {
        reason: sorted(nids if reason == STUDIED else set(nids) & losing)
        for reason, nids in held.items()
    }
    return sorted(delete), {reason: nids for reason, nids in listed.items() if nids}


def plan(rows: list, answers: dict, model: str) -> Plan:
    arrays = read_arrays(rows)
    split = frozenset(split_words(arrays))
    rejected, kept, unasked = [], [], []
    for nid, array in arrays.items():
        for word in counted_words(nid, array, split):
            answer = decision(word, answers, model)
            if answer == match_flags.DONT_MATCH:
                rejected.append(word)
            elif answer == match_flags.MATCH:
                kept.append(word)
            else:
                unasked.append(word)
    delete, held = notes_to_delete(rows, arrays, rejected)
    unlink = [word for word in rejected if not own_word(word)]
    own = [word for word in rejected if own_word(word)]
    return Plan(unlink, own, kept, unasked, delete, held, split)


# --- the report ---------------------------------------------------------------------------


def _by_word(words: list) -> list:
    counts: Counter = Counter((word.elem[2], word.elem[3]) for word in words)
    return ["%s [%s] x%d" % (form, reading, n) for (form, reading), n in counts.most_common()]


def _describe(row: dict) -> str:
    return "%d %s [%s]%s %s" % (
        row["nid"],
        row.get("vocab-key") or row.get("vocab") or "?",
        row.get("vocab-kana", ""),
        " studied" if row.get("studied") else "",
        (row.get("vocab-translation") or "")[:60],
    )


def report(planned: Plan, rows: list) -> list:
    by_nid = {row["nid"]: row for row in rows}
    notes = {word.nid for word in planned.unlink}
    links = sum(1 for word in planned.unlink if word.linked is not None)
    lines = [
        "%d words in %d word arrays go to dontmatch, %d of them linked to a note; %d notes are"
        " deleted." % (len(planned.unlink), len(notes), links, len(planned.delete)),
        "%d words stay as they are: the judge still matches them." % len(planned.kept),
    ]
    if planned.own:
        lines.append(
            "%d more the judge said dontmatch about are a note's own word and are not"
            " rewritten." % len(planned.own)
        )
    if planned.unasked:
        lines.append(
            "%d words have no answer yet and are left alone, with the notes they link: rerun"
            " with --ask." % len(planned.unasked)
        )

    # A word whose number or counter has no note leaves its sentence with nothing of it
    bare: Counter = Counter()
    for word in planned.unlink:
        for sub in word.elem[5]:
            if len(sub) > 1 and match_flags.matched_note_id(sub) is None:
                bare["%s [%s] %s, in %s" % (sub[2], sub[3], sub[4], word.elem[2])] += 1
    for title, words in (
        ("to dontmatch, by word", planned.unlink),
        ("a note's own word, not rewritten", planned.own),
        ("kept, by word", planned.kept),
        ("no answer yet", planned.unasked),
    ):
        if words:
            lines += ["", "--- %s ---" % title] + ["    %s" % w for w in _by_word(words)]
    lines += ["", "--- notes deleted (%d) ---" % len(planned.delete)]
    lines += ["    %s" % _describe(by_nid[nid]) for nid in planned.delete]
    for reason, nids in sorted(planned.held.items(), key=lambda item: -len(item[1])):
        lines += ["", "--- notes that lose a link and stay (%d): %s ---" % (len(nids), reason)]
        lines += ["    %s" % (_describe(by_nid[nid]) if nid in by_nid else nid) for nid in nids]
    if bare:
        lines += ["", "--- parts of an unlinked word that have no note themselves ---"]
        lines += ["    %s x%d" % (part, n) for part, n in bare.most_common()]
    return lines


# --- writing (over AnkiConnect) -----------------------------------------------------------


class Write(NamedTuple):
    nid: int
    before: str
    after: str


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def plan_writes(
    nids: list, infos: list, answers: dict, model: str, split: frozenset = frozenset()
) -> tuple:
    """The rewrites of the notes as Anki holds them now, and why a note is left alone."""
    writes, refused = [], []
    live = {info["noteId"]: info for info in infos if info}
    for nid in nids:
        if nid not in live:
            refused.append("nid %d: no such note now" % nid)
            continue
        before = live[nid].get("fields", {}).get(ARRAY_FIELD, {}).get("value", "") or ""
        array = match_flags.decode_word_array(before) if before.strip() else None
        if not array:
            refused.append("nid %d: %s holds no word array now" % (nid, ARRAY_FIELD))
            continue
        words = [
            word
            for word in counted_words(nid, array, split)
            if decision(word, answers, model) == match_flags.DONT_MATCH and not own_word(word)
        ]
        if not words:
            refused.append("nid %d: no word left that the judge said dontmatch about" % nid)
            continue
        for word in words:
            match_flags.set_dont_match(word.elem)
        writes.append(Write(nid, before, match_flags.format_word_array(array)))
    return writes, refused


def apply(client, planned: Plan, answers: dict, model: str, undo: Path, deleted: Path) -> tuple:
    """Rewrites the arrays, then deletes the notes nothing names any more; how many of each,
    and what was refused."""
    nids = sorted({word.nid for word in planned.unlink})
    infos = client.notes_info(nids) if nids else []
    writes, refused = plan_writes(nids, infos, answers, model, planned.split)
    for write in writes:
        with open(undo, "a", encoding="utf-8") as out:
            out.write(json.dumps(write._asdict(), ensure_ascii=False) + "\n")
        client.update_note_fields(write.nid, {ARRAY_FIELD: write.after})
    # AnkiConnect answers without an error for a note it did not update, which is what
    # happens to one open in the browser's editor
    written = [write.nid for write in writes]
    landed = {
        info["noteId"]: info.get("fields", {}).get(ARRAY_FIELD, {}).get("value")
        for info in (client.notes_info(written) if written else [])
        if info
    }
    for write in writes:
        if landed.get(write.nid) != write.after:
            refused.append(
                "nid %d: the rewrite did not land; is the note open in an editor?" % write.nid
            )

    # The dump is as old as the dump and a rewrite above may have been refused, so Anki is
    # asked again about everything a deletion rests on: the tag, the reviews, who names it
    doomed, named_by = {}, {}
    ids = ",".join(str(nid) for nid in planned.delete)
    studied = set(client.find_notes("-is:new nid:%s" % ids)) if planned.delete else set()
    for info in client.notes_info(planned.delete) if planned.delete else []:
        if not info:
            continue
        nid = info["noteId"]
        if ADDED_TAG not in info.get("tags", []):
            refused.append("nid %d: not deleted, it no longer has the tag %s" % (nid, ADDED_TAG))
        elif nid in studied:
            refused.append("nid %d: not deleted, it has been studied since the dump" % nid)
        else:
            doomed[nid] = info
            # Its own array names it still: a note's own word is not rewritten
            named_by[nid] = set(client.find_notes('"%s:*%d*"' % (ARRAY_FIELD, nid))) - {nid}
    # A note that stays after all keeps the notes its array names, so until none is left
    while True:
        staying = {nid: named_by[nid] - set(doomed) for nid in doomed}
        staying = {nid: names for nid, names in staying.items() if names}
        if not staying:
            break
        for nid, names in sorted(staying.items()):
            refused.append(
                "nid %d: not deleted, still named in the array of %s"
                % (nid, ", ".join(str(n) for n in sorted(names)))
            )
            del doomed[nid]
    # Everything a note held goes to disk before the note goes
    with open(deleted, "a", encoding="utf-8") as out:
        for info in doomed.values():
            out.write(json.dumps(info, ensure_ascii=False) + "\n")
    if doomed:
        client.delete_notes(sorted(doomed))
    return len(writes), len(doomed), refused


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
        if entry.nid not in now:
            refused.append("nid %d: the note is gone, nothing to put back" % entry.nid)
            continue
        if now[entry.nid] != entry.after:
            refused.append("nid %d: the word array changed since, left as is" % entry.nid)
            kept.append(entry)
            continue
        client.update_note_fields(entry.nid, {ARRAY_FIELD: entry.before})
        now[entry.nid] = entry.before
        reverted += 1
    text = "".join(json.dumps(e._asdict(), ensure_ascii=False) + "\n" for e in reversed(kept))
    undo.write_text(text, encoding="utf-8")
    return reverted, refused


# --- putting links back after the rules changed -------------------------------------------


def _field(info: dict) -> str:
    return info.get("fields", {}).get(ARRAY_FIELD, {}).get("value", "") or ""


def _links_a_note(match_data: list) -> bool:
    """Whether the match_data names a note, a placeholder's negative id included."""
    return bool(match_data) and isinstance(match_data[0], int)


def turned_words(entries: list, infos: list) -> tuple:
    """What an earlier run turned to dontmatch, as Anki holds it now: `[(Word, the match_data
    it had)]` for each such element that still is `["dontmatch"]`, `{note id: (field text,
    decoded array)}` of the notes those words are elements of, and why an element is skipped.

    An element is found by its place in the array (`iter_words` order) and must still be the
    same word: an array regenerated since is left alone."""
    live = {info["noteId"]: _field(info) for info in infos if info}
    turned, arrays, skipped = [], {}, []
    for entry in entries:
        nid = entry["nid"]
        before = match_flags.decode_word_array(entry["before"]) or []
        after = match_flags.decode_word_array(entry["after"]) or []
        took = {
            index: b[4]
            for index, ((_, b), (_, a)) in enumerate(
                zip(match_flags.iter_words(before), match_flags.iter_words(after))
            )
            if b[4] != a[4] and a[4] == [match_flags.DONT_MATCH]
        }
        if not took:
            continue
        if nid not in live:
            skipped.append("nid %d: the note is gone" % nid)
            continue
        if nid not in arrays:
            array = match_flags.decode_word_array(live[nid]) if live[nid].strip() else None
            if not array:
                skipped.append("nid %d: %s holds no word array now" % (nid, ARRAY_FIELD))
                continue
            arrays[nid] = (live[nid], array)
        words = list(_walk(arrays[nid][1]))
        was = [elem for _, elem in match_flags.iter_words(before)]
        for index, data in sorted(took.items()):
            same = index < len(words) and words[index][0][2:4] == was[index][2:4]
            if not same or words[index][0][4] != [match_flags.DONT_MATCH]:
                skipped.append(
                    "nid %d: %s is not as the run left it" % (nid, was[index][2])
                )
                continue
            elem, parents, sentence = words[index]
            group = judge.rule_group(elem, parents)
            prompt = judge.word_prompt(**judge.word_inputs(elem, sentence, parents, group))
            turned.append((Word(nid, elem, prompt, sentence, None), data))
    return turned, arrays, skipped


def plan_restore(turned: list, answers: dict, model: str) -> tuple:
    """The turned words the judge matches under the rules as they are now, with the
    match_data to give back; those it still turns down; and those with no answer yet."""
    back, stay, unasked = [], [], []
    for word, data in turned:
        answer = decision(word, answers, model)
        if answer == match_flags.MATCH:
            back.append((word, data))
        elif answer == match_flags.DONT_MATCH:
            stay.append(word)
        else:
            unasked.append(word)
    return back, stay, unasked


def restore(client, entries: list, arrays: dict, back: list, undo: Optional[Path]) -> tuple:
    """Gives the words in `back` their links again, in the live arrays of `arrays`; a word
    whose note is gone goes to `["match"]`, for match_words_to_notes to find or make its
    note. Written only with `undo`, whose entries then follow what Anki holds, so that
    `--revert` still knows the notes. How many arrays, what each word went back to, and what
    did not land."""
    linked = sorted({data[0] for _, data in back if _links_a_note(data)})
    found = client.notes_info(linked) if linked else []
    gone = {nid for nid, info in zip(linked, found) if not info}
    put = []
    for word, data in back:
        missing = _links_a_note(data) and data[0] in gone
        word.elem[4] = [match_flags.MATCH] if missing else list(data)
        put.append((word, word.elem[4]))
    texts = {
        nid: (arrays[nid][0], match_flags.format_word_array(arrays[nid][1]))
        for nid in sorted({word.nid for word, _ in back})
    }
    if undo is None:
        return len(texts), put, []
    for nid, (_, new) in texts.items():
        client.update_note_fields(nid, {ARRAY_FIELD: new})
    landed = {info["noteId"]: _field(info) for info in client.notes_info(list(texts)) if info}
    failed = [
        "nid %d: the write did not land; is the note open in an editor?" % nid
        for nid, (_, new) in texts.items()
        if landed.get(nid) != new
    ]
    kept = []
    for entry in entries:
        old, new = texts.get(entry["nid"], (None, None))
        if landed.get(entry["nid"]) == new and entry["after"] == old:
            # By the arrays, not the text: a field is written out anew, laid out as
            # format_word_array lays it out, which the text it had may not have been
            if arrays[entry["nid"]][1] == match_flags.decode_word_array(entry["before"]):
                continue  # all of it is back: nothing left to revert
            entry = {**entry, "after": new}
        kept.append(entry)
    tmp = undo.with_name(undo.name + ".tmp")
    tmp.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in kept), encoding="utf-8"
    )
    tmp.replace(undo)
    return len(texts) - len(failed), put, failed


def restore_report(put: list, stay: list, unasked: list, skipped: list) -> list:
    rematch = [word for word, data in put if data == [match_flags.MATCH]]
    lines = [
        "%d words get their link back, %d of them as [\"match\"] because their note is gone;"
        " %d stay dontmatch." % (len(put), len(rematch), len(stay))
    ]
    if unasked:
        lines.append("%d words have no answer yet: rerun with --ask." % len(unasked))
    for title, words in (
        ("back to their note, by word", [w for w, d in put if d != [match_flags.MATCH]]),
        ('back to ["match"], their note is gone', rematch),
        ("still dontmatch, by word", stay),
        ("no answer yet", unasked),
    ):
        if words:
            lines += ["", "--- %s ---" % title] + ["    %s" % w for w in _by_word(words)]
    if skipped:
        lines += ["", "--- skipped ---"] + ["    %s" % line for line in skipped]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="re-dump the notes over AnkiConnect")
    parser.add_argument("--ask", action="store_true", help="send the words with no answer yet")
    parser.add_argument("--workers", type=int, default=2, help="requests at once")
    parser.add_argument("--model", default="", help="instead of the configured judge model")
    parser.add_argument("--answers", type=Path, default=ANSWERS)
    parser.add_argument("--undo", type=Path, default=UNDO)
    parser.add_argument("--deleted", type=Path, default=DELETED)
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    parser.add_argument(
        "--restore", action="store_true", help="put back what the rules as they are now match"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", action="store_true")
    args = parser.parse_args()
    if args.restore and args.revert:
        parser.error("--restore puts some links back, --revert all of them: one or the other")

    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    try:
        if args.revert:
            reverted, refused = revert(client, args.undo)
            print("\n".join(refused + ["reverted %d word arrays" % reverted]))
            return 0
        if args.restore:
            entries = read_jsonl(args.undo)
            nids = sorted({entry["nid"] for entry in entries})
            infos = client.notes_info(nids) if nids else []
            turned, arrays, skipped = turned_words(entries, infos)
            op, _, _, config = load_judge()
            model = args.model or op.judge_model(config)
            back, stay, unasked = plan_restore(turned, read_answers(args.answers), model)
            if args.ask and unasked:
                prompts = sorted({word.prompt for word in unasked})
                print("asking %s about %d words" % (model, len(prompts)), file=sys.stderr)
                answered, failed = ask(prompts, model, args.workers, args.answers)
                print("%d answered, %d failed" % (answered, failed), file=sys.stderr)
                back, stay, unasked = plan_restore(turned, read_answers(args.answers), model)
            undo = args.undo if args.apply else None
            written, put, failed_writes = restore(client, entries, arrays, back, undo)
            lines = restore_report(put, stay, unasked, skipped + failed_writes)
            RESTORE_REPORT.parent.mkdir(parents=True, exist_ok=True)
            RESTORE_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(lines[0])
            print("report in %s" % RESTORE_REPORT)
            if args.apply:
                print("rewrote %d word arrays" % written)
            else:
                print("check it, then rerun with --restore --apply")
            return 0
        if args.fetch:
            print("%d notes -> %s" % (vocab_dupes.fetch(), vocab_dupes.DUMP))
        rows = vocab_dupes.read_dump()
        op, _, _, config = load_judge()
        model = args.model or op.judge_model(config)
        planned = plan(rows, read_answers(args.answers), model)
        if args.ask and planned.unasked:
            prompts = sorted({word.prompt for word in planned.unasked})
            print("asking %s about %d words" % (model, len(prompts)), file=sys.stderr)
            answered, failed = ask(prompts, model, args.workers, args.answers)
            print("%d answered, %d failed" % (answered, failed), file=sys.stderr)
            planned = plan(rows, read_answers(args.answers), model)
        lines = report(planned, rows)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines[:3]))
        if not args.apply:
            print("report in %s" % REPORT)
            print("check it, then rerun with --apply")
            return 0
        answers = read_answers(args.answers)
        written, gone, refused = apply(client, planned, answers, model, args.undo, args.deleted)
        print("\n".join(refused))
        print("rewrote %d word arrays, deleted %d notes" % (written, gone))
        print("refused %d" % len(refused))
        print("old field text in %s (--revert puts it back)" % args.undo)
        print("what the deleted notes held in %s" % args.deleted)
        return 0
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
