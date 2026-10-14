"""Notes whose own word the generator has only inside a word it leaves whole: the note is about
毛羽 and its sentence has 毛羽立ち, one verb to Sudachi, so no word of the array is the note's,
nothing can be linked to it and the card has no piece to mark.

For each such word the report says whether the generator's own checks find two words there. A
verb or adjective that `splits` can go on the config's `split_compound_words`, by the dictionary
form the report gives it, and that is what the list is decided from: what the collection's notes
are about. The sentence's own <b> is no substitute, though it marks the same word on the notes
that still have one: the generator does not read it, and a kanjified span cuts it short on many
notes (the note for 口ごもる has only 口 in bold).

    py -3.10 word_array/research/words_inside_words.py [--query SEARCH] [--out FILE]

Reads the notes of every note type the config names a sentence and a kanjified word field for,
through AnkiConnect, read only. `--query` narrows them with an Anki search. The sentences are
analyzed with no compound listed, so one the config lists already is reported like the rest,
and marked.
"""

import argparse
from collections import defaultdict
from typing import Iterator, NamedTuple

import anki_connect
from _bootstrap import ADDON_ROOT, load, load_root

generator = load("generator")
html_stripping = load_root("html_stripping")

WORD_FIELD = "word_kanjified_field"
CHUNK = 500
SPLITS = "splits"
INFLECTING = "verb or adjective"


class Inside(NamedTuple):
    kind: str  # INFLECTING, or the head's part of speech
    form: str  # the one-unit word's dictionary form, as an array shows it and the config lists it
    first: str  # the two sides: written text, the second of a verb or adjective as its
    second: str  # dictionary form
    side: str  # which of the two is the note's word: "first" or "second"
    verdict: str  # SPLITS, or why the generator has no two words here


def _has_word(array: list, word: str) -> bool:
    return any(len(e) > 1 and (e[2] == word or _has_word(e[5], word)) for e in array)


def _leaves(words: list) -> Iterator:
    for w in words:
        if w.subs:
            yield from _leaves(w.subs)
        elif w.kind != "punct":
            yield w


def _verdict(tm, w, i: int) -> str:
    """Whether the generator's own checks find two words in `w` at natural index `i`."""
    not_read = "is no word JMdict reads so, or is a lone on'yomi kanji"
    if not tm.can_split(i):
        return "the furigana gives the two sides no reading each"
    if w.head.pos[0] in generator.INFLECTING:
        first = generator._stem_piece(tm, w.start, i) or generator._piece(tm, w.start, i, False)
        if not first:
            return f"the first {not_read}"
        if not generator._inflecting_piece(tm, w, i):
            return "the second is no verb or adjective the compound ends on"
        return SPLITS
    if not generator._piece(tm, w.start, i, second=False):
        return f"the first {not_read}"
    if not generator._piece(tm, i, w.end, second=True):
        return f"the second {not_read}"
    return "two words the generator leaves whole: okurigana, a name, a number"


def words_inside(sentence: str, word: str) -> list[Inside]:
    """Each one-unit word of the sentence that `word` is one side of, by the dictionary form an
    array shows it in; none when the sentence has `word` as a word of the array anyway. No
    compound is split for it: the report is what the config's list is decided from, whatever
    it holds by now."""
    analysis = generator.analyze(sentence)
    if not word or _has_word(analysis.array, word):
        return []
    tm = analysis.text_map
    out = []
    for w in _leaves(analysis.final):
        inflecting = w.head.pos[0] in generator.INFLECTING
        whole = generator.dict_form(tm, w)
        # Two sides -> verdict. Several natural indices can stand for one place in the written
        # text (a <k> group goes to the tokenizer as its reading): the one that splits counts
        verdicts: dict[tuple[str, str], str] = {}
        # Past the head a verb has only its inflection, which is no side of a compound
        for i in range(w.start + 1, w.head.end if inflecting else w.end):
            first = tm.written_form(w.start, i)
            if inflecting:
                second = whole[len(first) :] if whole.startswith(first) else ""
            else:
                second = tm.written_form(i, w.end)
            if not first or not second or word not in (first, second):
                continue
            if tm.can_split(i) or (first, second) not in verdicts:
                verdicts[first, second] = _verdict(tm, w, i)
        if not verdicts:
            continue
        kind = INFLECTING if inflecting else w.head.pos[0]
        form = generator.shown_form(tm, w, generator.dict_reading(tm, w))[0]
        for (first, second), verdict in verdicts.items():
            side = "first" if word == first else "second"
            out.append(Inside(kind, form, first, second, side, verdict))
    return out


def notes(client, config: dict, query: str) -> Iterator[tuple[int, str, str]]:
    """(note id, sentence without its context sentences, kanjified word) of every note of the
    note types the config names both fields for."""
    for model, value in config.items():
        if not isinstance(value, dict):
            continue
        sentence_field, word_field = value.get(anki_connect.SENTENCE_FIELD), value.get(WORD_FIELD)
        if not sentence_field or not word_field:
            continue
        nids = client.find_notes(f'note:"{model}" {query}'.strip())
        for at in range(0, len(nids), CHUNK):
            for info in client.notes_info(nids[at : at + CHUNK]):
                fields = info.get("fields", {}) if info else {}
                sentence = fields.get(sentence_field, {}).get("value", "")
                word = generator.match_flags.plain_text(fields.get(word_field, {}).get("value", ""))
                if sentence.strip() and word:
                    yield info["noteId"], html_stripping.strip_context_sentences(sentence), word
            print(f"{model}: {min(at + CHUNK, len(nids))}/{len(nids)}")


def report(found: dict[Inside, list[int]], count: int, listed: frozenset[str]) -> list[str]:
    lines = [
        f"{count} notes; {sum(len(n) for n in found.values())} have their word only inside a "
        f"one-unit word, {len(found)} such words"
    ]
    for kind in sorted({m.kind for m in found}, key=lambda k: (k != INFLECTING, k)):
        rows = [(m, nids) for m, nids in found.items() if m.kind == kind]
        lines += ["", f"== {kind}: {sum(len(nids) for _, nids in rows)} notes"]
        for m, nids in sorted(rows, key=lambda r: (r[0].verdict != SPLITS, r[0].verdict, r[0])):
            nid_list = " nid:".join(map(str, nids[:3]))
            lines.append(f"{len(nids):4} {describe(m, listed)}  nid:{nid_list}")
    return lines


def describe(m: Inside, listed: frozenset[str]) -> str:
    """One word of the report, marked when the config's compounds to split, `listed`, have it:
    with `splits` for its verdict a regeneration splits it already, with any other the list
    does nothing for it."""
    sides = f"[{m.first}] {m.second}" if m.side == "first" else f"{m.first} [{m.second}]"
    mark = f", on {generator.SPLIT_COMPOUNDS_KEY}" if m.form in listed else ""
    return f"{m.form}  {sides}  {m.verdict}{mark}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="", help="an Anki search the notes must match too")
    parser.add_argument("--anki-connect", default=anki_connect.URL)
    parser.add_argument(
        "--out", default=str(ADDON_ROOT / "output" / "words_inside_words_report.txt")
    )
    args = parser.parse_args()
    client = anki_connect.AnkiConnect(args.anki_connect, timeout=300)
    config = anki_connect.load_config()
    listed = generator.split_compounds_of(config)
    found: dict[Inside, list[int]] = defaultdict(list)
    count = 0
    try:
        for nid, sentence, word in notes(client, config, args.query):
            count += 1
            for inside in words_inside(sentence, word):
                found[inside].append(nid)
    except anki_connect.AnkiConnectError as e:
        print(e)
        return 1
    lines = report(found, count, listed)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(lines[0])
    for m in sorted(found):
        if m.kind == INFLECTING and m.verdict == SPLITS:
            print(f"  {describe(m, listed)}  x{len(found[m])}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
