"""The kanjify golden set's work inventory, from the dump (`kanjify_golden_dump.py`).

Step 1 decides a word, step 2 labels sentences with those decisions. The words are the
kanjify survey's (`kanjify_survey.word_classes`) over the collection's current kanjified
fields: left-kana (kanjified in some sentences, kana in others), meaning (kanjified with
different kanji) and never (always kana though JMdict spells it with kanji). Writes to
`evals/kanjify_golden/inventory/`:

  words.jsonl      one step 1 word: `wid` (stable: a hash of the word), the word and its pos,
                   its kana, classes, counts, JMdict kanji choices, and every use (sentence
                   id, kana / kanjified / kanji, the kanji, the sentence with the word marked)
  sentences.jsonl  one input sentence: `sid`, the sentence, its notes, the current kanjified
                   field (for step 1 and reports, never shown to step 2), its step 1 words,
                   `hand` when it is one of the hand-fixed labels, and what a program sees
                   wrong with its furigana (`kanjify_golden.furigana_suspects`)
  batches.jsonl    step 2's batches: sentences grouped by the rarest step 1 word each holds, so
                   a batch's prompt carries few word decisions; `d...` batches label a 5%
                   sample a second time, among other sentences, for the agreement report
  pilot.json       the pilot's sentences: half hand-fixed, half holding step 1 words

    py -3.10 word_array/research/kanjify_golden_inventory.py [--batch-size 20] [--pilot 30]

Batches with hand-fixed sentences come first, so calibration figures come early in a run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from _bootstrap import load, load_root

import kanjify_golden as golden
import kanjify_survey as survey

text_map = load("text_map")
html_stripping = load_root("html_stripping")



def word_id(key: tuple[str, str]) -> str:
    return "w" + hashlib.sha1(f"{key[0]}|{key[1]}".encode("utf-8")).hexdigest()[:8]


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def current_fields(dump: Path) -> dict[str, str]:
    """Per input sentence, its notes' most common kanjified field."""
    fields: dict[str, Counter] = defaultdict(Counter)
    for note in golden.read_jsonl(dump):
        if note["sentence"]:
            fields[note["sentence"]][note["kanjified"]] += 1
    return {s: c.most_common(1)[0][0] for s, c in fields.items()}


def inconsistent(word: dict) -> int:
    """The uses that disagree with the word's majority: the smaller of its kana and kanjified
    sides, plus the kanjified uses in other than its commonest kanji. A never-kanjified word
    counts all its uses. Step 1 takes the words in this order, since they change the most."""
    spelled = word["kanjified"]
    total = sum(spelled.values())
    if not total:
        return word["kana_uses"]
    return min(word["kana_uses"], total) + total - max(spelled.values())


def marked(natural: str, start: int, end: int) -> str:
    return f"{natural[:start]}【{natural[start:end]}】{natural[end:]}"


def batch(sids: list[str], size: int) -> list[list[str]]:
    return [sids[i : i + size] for i in range(0, len(sids), size)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", type=Path, default=golden.DUMP)
    parser.add_argument("--out", type=Path, default=golden.INVENTORY)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--dup-every", type=int, default=20, help="1 in N sentences twice")
    parser.add_argument("--pilot", type=int, default=30)
    parser.add_argument("--min-uses", type=int, default=3)
    args = parser.parse_args()

    sentences = golden.read_sentences(args.dump)
    current = current_fields(args.dump)
    hand = golden.hand_labels(sentences)
    rows = []
    for s in sentences:
        raw = current[s.sentence]
        rows.append((s.nids, raw))
    flat, _, failed = survey.analyze(rows)
    classes = survey.word_classes(flat, survey.choice_finder(), args.min_uses)
    choices = survey.choice_finder()

    naturals: dict[int, str] = {}

    def natural(row: int) -> str:
        if row not in naturals:
            raw = rows[row][1]
            text = html_stripping.strip_context_sentences(raw) if "<i>" in raw else raw
            naturals[row] = text_map.build(text).natural
        return naturals[row]

    keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key in classes.left:
        keys[key].add("left-kana")
    for key, spelled, _ in classes.meaning:
        # the survey's "rarely kanjified" test: か, だ, ます with a few kanjified uses among
        # thousands are Sudachi cutting 何か, 然だ... differently, not a word to decide
        if sum(spelled.values()) >= max(2, 0.02 * len(classes.kana.get(key, []))):
            keys[key].add("meaning")
    for key in classes.never_one + classes.never_several:
        keys[key].add("never")

    words: list[dict] = []
    sentence_words: dict[int, set[str]] = defaultdict(set)
    for key, cls in keys.items():
        wid = word_id(key)
        kanjified = classes.kanjified.get(key, [])
        kana = classes.kana.get(key, [])
        uses = []
        for kind, toks in (("kanjified", kanjified), ("kana", kana)):
            for t in toks:
                sentence_words[t.row].add(wid)
                uses.append(
                    {
                        "sid": sentences[t.row].sid,
                        "kind": kind,
                        "kanji": t.kanji,
                        "text": marked(natural(t.row), t.morph.start, t.morph.end),
                    }
                )
        sample = (kana or kanjified)[0]
        cs = choices(sample)
        words.append(
            {
                "wid": wid,
                "word": key[0],
                "pos": key[1],
                "kana": survey.kana_of(sample),
                "classes": sorted(cls),
                "kanjified": dict(Counter(t.kanji for t in kanjified).most_common()),
                "kana_uses": len(kana),
                "kanji_uses": classes.kanji[key],
                "jmdict": cs,
                "meaning_tag": survey.meaning_tag(cs),
                "uses": uses,
            }
        )
    words.sort(key=lambda w: -inconsistent(w))
    use_count = {w["wid"]: len(w["uses"]) for w in words}

    out_sentences: list[dict] = []
    for i, s in enumerate(sentences):
        out_sentences.append(
            {
                "sid": s.sid,
                "sentence": s.sentence,
                "nids": s.nids,
                "current": current[s.sentence],
                "words": sorted(sentence_words.get(i, ()), key=lambda w: use_count[w]),
                "hand": s.sid in hand,
                "furigana_suspects": golden.furigana_suspects(s.sentence),
            }
        )

    # step 2: grouped by the rarest step 1 word, homophones next to each other (by its kana)
    kana_of = {w["wid"]: w["kana"] for w in words}
    with_words = [s for s in out_sentences if s["words"]]
    with_words.sort(key=lambda s: (kana_of[s["words"][0]], s["words"][0], s["sid"]))
    plain = sorted((s for s in out_sentences if not s["words"]), key=lambda s: _hash(s["sid"]))
    by_sid = {s["sid"]: s for s in out_sentences}
    batches: list[dict] = []
    for sids in batch([s["sid"] for s in with_words], args.batch_size) + batch(
        [s["sid"] for s in plain], args.batch_size
    ):
        batches.append({"sids": sids, "dup": False})
    dup = sorted(
        (s["sid"] for s in out_sentences if int(_hash(s["sid"] + "dup"), 16) % args.dup_every == 0),
        key=lambda sid: _hash(sid + "order"),
    )
    batches += [{"sids": sids, "dup": True} for sids in batch(dup, args.batch_size)]
    for b in batches:
        b["hand"] = sum(by_sid[sid]["hand"] for sid in b["sids"])
        b["words"] = sorted({w for sid in b["sids"] for w in by_sid[sid]["words"]})
    batches.sort(key=lambda b: (-b["hand"], b["dup"]))
    n = m = 0
    for b in batches:
        if b["dup"]:
            m += 1
            b["bid"] = f"d{m:04d}"
        else:
            n += 1
            b["bid"] = f"b{n:04d}"

    hand_sids = sorted((s["sid"] for s in out_sentences if s["hand"]), key=lambda x: _hash(x + "pilot"))
    word_sids = sorted(
        (s["sid"] for s in with_words if not s["hand"]), key=lambda x: _hash(x + "pilot")
    )
    pilot = hand_sids[: args.pilot // 2] + word_sids[: args.pilot - args.pilot // 2]

    args.out.mkdir(parents=True, exist_ok=True)
    golden.write_jsonl(args.out / "words.jsonl", words)
    golden.write_jsonl(args.out / "sentences.jsonl", out_sentences)
    golden.write_jsonl(args.out / "batches.jsonl", ({"bid": b.pop("bid"), **b} for b in batches))
    (args.out / "pilot.json").write_text(json.dumps({"sids": pilot}, indent=1) + "\n")
    meta = {
        "dump": golden.relative(args.dump),
        "dump_sha1": hashlib.sha1(args.dump.read_bytes()).hexdigest()[:12],
        "sentences": len(out_sentences),
        "failed": failed,
        "hand": len(hand),
        "words": len(words),
        "word_classes": dict(Counter(c for w in words for c in w["classes"])),
        "sentences_with_words": len(with_words),
        "batches": sum(1 for b in batches if not b["dup"]),
        "dup_batches": sum(1 for b in batches if b["dup"]),
        "dup_sentences": len(dup),
        "pilot": len(pilot),
        "furigana_suspects": sum(1 for s in out_sentences if s["furigana_suspects"]),
    }
    (args.out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
