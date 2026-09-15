"""Collection-wide kanjification survey (task 22): the kanjify audit's word classes over every
note sentence in the migration export (`output/extract_words_migration_data.jsonl`, the
`sentence-kanjified-furigana` field of the whole collection), not just the checked rows.

  policy     grammar uses kanjified that the kanjify policy (task 20) wants kana, counted per
             class and per helper verb (て見る, て居る, で有る...), with how often the same use
             is already kana. Fixes -> `output/kanjify_survey_fixes.jsonl` (kanjify_fix.py rows).
  left-kana  a word kanjified in some sentences, left kana in others (policy-kana uses aside),
             with the kanji used; kana homophones (JMdict entries of its pos) tagged HOMOPHONES,
             one entry with several kanji CHOICES. A word kanjified once or in under 2% of its
             uses is listed apart as "rarely kanjified" (miscuts of の/た, one-off slips).
  meaning    a word kanjified with different kanji (有る/在る, 依る/因る).
  never      content words always kana though JMdict spells them with kanji: one kanji choice
             (the canonical key would kanjify it), or several (homophones, よる: 夜/寄る/依る;
             one entry's choices, 依る/因る) that depend on meaning.

Left-kana and meaning words also go to `output/kanjify_survey_tasks.jsonl` in the audit's
task format, so `kanjify_agents/make_prompts.py --tasks` renders them for task 23's LLM pass.
`<i>` context is stripped before counting (it repeats other notes' sentences).

    py -3.10 word_array/research/kanjify_survey.py [-n COUNT] [--min-uses N]
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

from _bootstrap import ADDON_ROOT, load, load_root

import kanjify_audit as audit

generator = load("generator")
jmdict = load("jmdict_index")
html_stripping = load_root("html_stripping")

OUTPUT = ADDON_ROOT / "output"
EXPORT = OUTPUT / "extract_words_migration_data.jsonl"
CONTENT_POS = {
    "名詞", "代名詞", "動詞", "形容詞", "形状詞", "副詞", "連体詞", "接続詞", "感動詞", "接頭辞",
    "接尾辞",
}  # fmt: skip
UNUSED_KANJI = {"oK", "sK"}  # outdated and search-only spellings aren't a choice
# Sudachi pos -> JMdict pos codes (a code or its "code-..." variants) a homophone must have, so
# もう (adverb) isn't 猛/毛 and だけ (particle) isn't 岳
JMDICT_POS = {
    "名詞": ("n", "ctr", "suf", "n-suf", "adj-no"), "代名詞": ("pn", "n"), "動詞": ("v1", "v5",
    "vk", "vs", "vz", "v2", "v4"), "形容詞": ("adj-i", "adj-ix"), "形状詞": ("adj-na", "adj-no",
    "adj-t"), "副詞": ("adv", "adv-to"), "連体詞": ("adj-pn", "adj-f"), "接続詞": ("conj",),
    "感動詞": ("int",), "接尾辞": ("suf", "n-suf", "ctr"), "接頭辞": ("pref",), "助詞": ("prt",),
    "助動詞": ("aux", "aux-v", "aux-adj", "cop"),
}  # fmt: skip


def pos_fits(codes, pos0: str) -> bool:
    wanted = JMDICT_POS.get(pos0)
    if wanted is None:
        return True
    return any(c == w or c.startswith(w) for c in codes for w in wanted)


def read_rows(path: Path) -> list[tuple[list[int], str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            rows.append((list(d.get("nids") or []), d["sentence"]))
    return rows


def kanji_choices(kana: str, pos0: str, lookup: Callable, spellings: Callable) -> list[list[str]]:
    """Per JMdict entry read `kana` with a pos that fits Sudachi's, its kanji spellings in use,
    one per set of kanji (積り and 積もり are one), entries without kanji left out: よる (verb)
    -> [[寄る], [依る, 因る, 拠る, 由る]]."""
    out, seen = [], set()
    for kebs, rebs, codes in lookup(kana):
        if kana not in rebs or (kebs, rebs) in seen or not pos_fits(codes, pos0):
            continue
        seen.add((kebs, rebs))
        info, restr, _ = spellings(kebs, rebs)
        allowed = restr.get(kana, kebs)
        sets, choice = set(), []
        for k in kebs:
            kanji = frozenset(audit.KANJI_RE.findall(k))
            if (
                k in allowed
                and kanji
                and not UNUSED_KANJI & info.get(k, set())
                and kanji not in sets
            ):
                sets.add(kanji)
                choice.append(k)
        if choice:
            out.append(choice)
    return out


def meaning_tag(choices: list[list[str]]) -> str:
    """HOMOPHONES for several entries with kanji, CHOICES for one entry with several kanji
    choices (both depend on meaning), "" otherwise."""
    if len(choices) > 1:
        return "HOMOPHONES"
    return "CHOICES" if any(len(c) > 1 for c in choices) else ""


def kana_of(tok: audit.Tok) -> str:
    """The dictionary form a kana word is looked up by (よっ -> よる)."""
    return generator.to_hiragana(tok.morph.lemma)


def policy_breakdown(toks: list[audit.Tok]) -> dict[tuple[str, str], Counter]:
    """(policy class, word) -> kinds: how often each grammar use is kanjified/kana/kanji."""
    out: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for t in toks:
        if t.policy:
            out[(t.policy, t.morph.norm)][t.kind] += 1
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=Path, default=EXPORT)
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT rows")
    parser.add_argument("--min-uses", type=int, default=3, help="never-kanjified words shown")
    parser.add_argument("--out", type=Path, default=OUTPUT / "kanjify_survey_report.txt")
    parser.add_argument("--fixes", type=Path, default=OUTPUT / "kanjify_survey_fixes.jsonl")
    parser.add_argument("--tasks", type=Path, default=OUTPUT / "kanjify_survey_tasks.jsonl")
    args = parser.parse_args()
    rows = read_rows(args.rows)[: args.n or None]

    flat, fixes, failed = [], [], 0
    for i, (nids, raw) in enumerate(rows):
        text = html_stripping.strip_context_sentences(raw) if "<i>" in raw else raw
        try:
            toks = audit.analyze_row(i, text)
            if any(t.kind == "kanjified" and t.policy for t in toks):
                raw_toks = toks if text == raw else audit.analyze_row(i, raw)
                after, row_fixes = audit.row_fix(raw, raw_toks, {})
                if row_fixes and after != raw:
                    fixes.append(
                        {"row": i, "nids": nids, "before": raw, "after": after, "fixes": row_fixes}
                    )
        except Exception as e:  # survey only
            failed += 1
            print(f"[{i}] {type(e).__name__}: {e}")
            continue
        flat.extend(toks)
        if i % 2000 == 0:
            print(f"{i} rows")

    def where(t: audit.Tok) -> str:
        return f"[{t.row}] nid {','.join(map(str, rows[t.row][0])) or '-'}  {t.context}"

    choices_cache: dict[tuple[str, str], list[list[str]]] = {}

    def choices(t: audit.Tok) -> list[list[str]]:
        key = (kana_of(t), t.morph.pos[0])
        if key not in choices_cache:
            choices_cache[key] = kanji_choices(*key, jmdict.lookup, jmdict.spellings)
        return choices_cache[key]

    summary = [f"{len(rows)} rows from {args.rows} ({failed} failed)"]
    lines = []

    # policy
    breakdown = policy_breakdown(flat)
    policy_hits = [t for t in flat if t.kind == "kanjified" and t.policy]
    summary.append(
        f"policy kanjified: {len(policy_hits)} uses in "
        f"{len({t.row for t in policy_hits})} rows; fix rows {len(fixes)} -> {args.fixes.name}"
    )
    lines += ["", "== policy: grammar uses by class and word (kanjified / kana / kanji already)"]
    for name in audit.POLICY_CLASSES:
        keys = sorted(
            (k for k in breakdown if k[0] == name), key=lambda k: -breakdown[k]["kanjified"]
        )
        total = sum(breakdown[k]["kanjified"] for k in keys)
        summary.append(f"  {name}: {total} kanjified")
        lines.append(f"  -- {name}: {total} kanjified")
        for k in keys:
            c = breakdown[k]
            lines.append(
                f"    {k[1]}: kanjified {c['kanjified']}, kana {c['kana']}, kanji {c['kanji']}"
            )
            lines += [f"        {where(t)}" for t in policy_hits if (t.policy, t.morph.norm) == k][
                :3
            ]

    # per word, policy uses aside
    kanjified, kana, kanji = defaultdict(list), defaultdict(list), Counter()
    for t in flat:
        if t.policy:
            continue
        if t.kind == "kanjified" and t.kanji:  # kana inside <k> isn't this word kanjified
            kanjified[audit.word_key(t)].append(t)
        elif t.kind == "kana":
            kana[audit.word_key(t)].append(t)
        elif t.kind == "kanji":
            kanji[audit.word_key(t)] += 1

    def kana_note(ts: list[audit.Tok]) -> str:
        cs = choices(ts[0])
        return f"  JMdict {' | '.join('/'.join(c) for c in cs) or '-'}  {meaning_tag(cs)}".rstrip()

    def rare(k) -> bool:
        """Kanjified once, or in under 2% of its uses: mostly Sudachi cutting a kanjified
        sentence differently (の, た), sometimes a one-off slip (カメラ -> 写真機)."""
        return len(kanjified[k]) < max(2, 0.02 * len(kana[k]))

    both = [k for k in kanjified if k in kana]
    rares = sorted((k for k in both if rare(k)), key=lambda k: -len(kana[k]))
    left = sorted((k for k in both if not rare(k)), key=lambda k: -len(kana[k]))
    left_meaning = [k for k in left if meaning_tag(choices(kana[k][0]))]
    summary.append(
        f"left-kana: {len(left)} words, {sum(len(kana[k]) for k in left)} kana uses "
        f"({len(left_meaning)} words meaning-dependent)"
    )
    lines += ["", f"== left-kana: {len(left)} words kanjified in some sentences, kana in others"]
    for key in left:
        spelled = Counter(t.kanji for t in kanjified[key])
        lines.append(
            f"  {key[0]} ({key[1]}): kanjified {len(kanjified[key])} "
            f"({', '.join(f'{s} {n}' for s, n in spelled.most_common())}), kana {len(kana[key])}, "
            f"kanji {kanji[key]}" + kana_note(kana[key])
        )
        lines += [f"      kana {where(t)}" for t in kana[key][:3]]

    summary.append(
        f"rarely kanjified (not in tasks): {len(rares)} words, "
        f"{sum(len(kanjified[k]) for k in rares)} kanjified uses"
    )
    lines += ["", f"== rarely kanjified: {len(rares)} words (miscuts or one-off slips, no tasks)"]
    for key in rares:
        lines.append(
            f"  {key[0]} ({key[1]}): kana {len(kana[key])}, kanjified "
            + ", ".join(f"{t.kanji} {where(t)}" for t in kanjified[key][:2])
        )

    meaning = []
    for key, ts in kanjified.items():
        spelled = Counter(t.kanji for t in ts)
        if len(spelled) > 1:
            meaning.append((key, spelled, ts))
    meaning.sort(key=lambda m: -sum(m[1].values()))
    summary.append(f"meaning: {len(meaning)} words kanjified with different kanji")
    lines += ["", f"== meaning: {len(meaning)} words kanjified with different kanji"]
    for key, spelled, ts in meaning:
        lines.append(
            f"  {key[0]} ({key[1]}): " + ", ".join(f"{s} {n}" for s, n in spelled.most_common())
        )
        for s in spelled:
            lines += [f"      {s}  {where(t)}" for t in ts if t.kanji == s][:2]

    never = [
        k
        for k, ts in kana.items()
        if k not in kanjified
        and k[1] in CONTENT_POS
        and len(ts) >= args.min_uses
        and choices(ts[0])
        and not generator.KATAKANA_RE.search(ts[0].morph.surface)
    ]
    one = sorted((k for k in never if not meaning_tag(choices(kana[k][0]))),
                 key=lambda k: -len(kana[k]))  # fmt: skip
    several = sorted((k for k in never if k not in one), key=lambda k: -len(kana[k]))
    summary.append(
        f"never kanjified (>= {args.min_uses} uses, JMdict kanji): {len(one)} one choice "
        f"({sum(len(kana[k]) for k in one)} uses), {len(several)} meaning-dependent "
        f"({sum(len(kana[k]) for k in several)} uses)"
    )
    for title, keys in (
        ("one kanji choice", one),
        ("meaning-dependent (homophones or several kanji)", several),
    ):
        lines += ["", f"== never kanjified, {title}: {len(keys)} words"]
        for key in keys:
            lines.append(
                f"  {kana_of(kana[key][0])} {key[0]} ({key[1]}): kana {len(kana[key])}, "
                f"kanji {kanji[key]}" + kana_note(kana[key])
            )
            lines += [f"      {where(t)}" for t in kana[key][:2]]

    def item(t: audit.Tok) -> dict:
        return {"nids": rows[t.row][0], "row": t.row, "context": t.context}

    with open(args.tasks, "w", encoding="utf-8") as f:
        for key in left:
            task = {"class": "left-kana", "word": key[0], "pos": key[1]}
            task["kanjified"] = dict(Counter(t.kanji for t in kanjified[key]).most_common())
            task["items"] = [item(t) for t in kana[key]]
            task["kanjified_items"] = [{**item(t), "kanji": t.kanji} for t in kanjified[key]]
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
        for key, spelled, ts in meaning:
            task = {"class": "meaning", "word": key[0], "pos": key[1]}
            task["kanjified"] = dict(spelled.most_common())
            task["items"] = [{**item(t), "kanji": t.kanji} for t in ts]
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
    summary.append(f"tasks: {len(left) + len(meaning)} words -> {args.tasks.name}")

    with open(args.fixes, "w", encoding="utf-8") as f:
        for row in fixes:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    args.out.write_text("\n".join(summary + lines) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
