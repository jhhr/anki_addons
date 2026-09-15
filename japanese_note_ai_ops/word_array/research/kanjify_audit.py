"""Consistency audit of the hand-fixed kanjify labels against the kanjify policy (task 20).

Every label (the checked notes' kanjified sentence) is tokenized as the text map gives it: `<k>`
groups as the kana they were, so Sudachi reads each row as written before kanjifying and each
morph knows whether the label kanjified it, it was kanji already, or it stayed kana. Classes:

  policy     a kanjified word the policy wants kana: て-helper verbs, である, negation ない after
             く/では/じゃ/a verb, て-patterns (てほしい, てもいい, てはいけない...). Confirmed by
             Sudachi's parse; a helper verb Sudachi doesn't read as one is listed as "check".
  left-kana  a kana word (Sudachi normalized form + pos) kanjified in another row, not in a
             policy-kana use. Grouped by word: kanjified n / kana m.
  spelling   one word kanjified with different kanji (meaning: 有る/在る, the user's call) or the
             same kanji with the furigana cut differently (format: 此[この]/此[こ]の).
  mismatch   the label with every span back in kana isn't the source sentence (the op's check):
             "furigana" when it still reads the same (groups regrouped, kanji without <k>),
             "text" when the hand fix changed what it says.

Policy and format fixes go to `output/kanjify_audit_fixes.jsonl`, one row per note sentence:
{"nids", "before", "after", "fixes": [{"class", "k", "word", ...}]}. A policy fix turns only the
word's furigana groups into kana (`note_edits.span_kana`); a format fix takes the most common cut
(ties are listed, not fixed). Spans inside `<i>` context aren't numbered, so aren't fixed.

    py -3.10 word_array/research/kanjify_audit.py [--rows FILE] [-n COUNT]

Reads `output/kanjify_sentence_data.jsonl`, else the old fine-tuning files (no nids: fixes are
listed but can't be written to notes).
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

from _bootstrap import ADDON_ROOT, load

import note_edits

generator = load("generator")
text_map = load("text_map")

OUTPUT = ADDON_ROOT / "output"
EXPORT = OUTPUT / "kanjify_sentence_data.jsonl"
OLD_FILES = [
    OUTPUT / "kanjify_sentence_fine_tuning.jsonl",
    OUTPUT / "kanjify_sentence_fine_tuning_validation.jsonl",
]
B_TAG_RE = re.compile(r"</?b>")
TAG_RE = re.compile(r"<[^>]+>")
K_RE = re.compile(r"<k>(.*?)</k>", re.S)
NUMBER_FURI_RE = re.compile(r"(\d+)\[([^\]]+)\]([あ-ん]*)")
KANJI_RE = re.compile(r"[一-龯㐀-䶿々]")
KANJI_GROUP_RE = re.compile(r"[\d々ヶヵ〆一-龯㐀-䶿]+\[([^\]]*)\]")  # space before it or not
KANA_RE = re.compile(r"[ぁ-ゖァ-ヺー]")

HELPER_NORMS = {
    "居る", "有る", "見る", "来る", "行く", "呉れる", "仕舞う", "おく", "置く", "貰う", "頂く",
    "下さる", "上げる", "遣る", "参る", "いらっしゃる",
}  # fmt: skip
PATTERNS = {  # norm -> particles allowed between て and it ("" = none)
    "欲しい": {""},
    "良い": {"", "も"},
    "行く": {"は"},  # いけない
    "成る": {"は"},  # ならない
    "構う": {"も"},
    "駄目": {"は", "も"},
}
POLICY_CLASSES = ("て-helper", "である", "negation ない", "て-pattern", "check")


class Row(NamedTuple):
    nids: list[int]
    sentence: str  # the op's input, furigana sentence
    label: str  # the hand-fixed kanjified sentence


def read_rows(path: Optional[Path] = None) -> tuple[list[Row], str]:
    """Rows of the kanjify export, else of the old fine-tuning files; and what was read."""
    path = path or (EXPORT if EXPORT.exists() else None)
    if path is not None:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                rows.append(Row(list(d.get("nids") or []), d["sentence"], d["kanjified"]))
        return rows, str(path)
    rows = []
    for old in OLD_FILES:
        for line in old.read_text(encoding="utf-8").splitlines():
            if line.strip():
                msgs = json.loads(line)["messages"]
                sentence = msgs[1]["content"].rsplit("The sentence to process: ", 1)[1].strip()
                label = json.loads(msgs[2]["content"])["kanjified_sentence"]
                rows.append(Row([], sentence, label))
    return rows, " + ".join(str(p) for p in OLD_FILES) + " (no nids)"


def _as_reading(text: str) -> str:
    return KANJI_GROUP_RE.sub(lambda g: g[1], text)


def mismatch(sentence: str, label: str) -> Optional[str]:
    """None when the label with every `<k>` span as kana is the source sentence (the op's
    check: whitespace and `<b>` aside, a number given furigana the source lacks dropping it);
    "furigana" when they still read the same with all furigana as kana (groups outside `<k>`
    regrouped, a word kanjified without `<k>`), else "text"."""

    def number(m: re.Match) -> str:
        return m[0] if m[0] in sentence else m[1] + m[3]

    text = K_RE.sub(lambda m: _as_reading(m[1]), B_TAG_RE.sub("", label))
    text = re.sub(r"\s", "", NUMBER_FURI_RE.sub(number, text))
    source = re.sub(r"\s", "", B_TAG_RE.sub("", sentence))
    if text == source:
        return None
    return "furigana" if _as_reading(text) == _as_reading(source) else "text"


def _te_before(morphs: list, i: int, particles: set[str]) -> bool:
    """morphs[i] follows a connective て/で, with one of `particles` between ("" = none)."""
    j = i - 1
    if j >= 0 and morphs[j].surface in particles and morphs[j].pos[0] == "助詞":
        j -= 1
    elif "" not in particles:
        return False
    return (
        j >= 0 and morphs[j].surface in ("て", "で") and morphs[j].pos[:2] == ("助詞", "接続助詞")
    )


def _copula_de(morphs: list, j: int) -> bool:
    """morphs[j] is the copula で/じゃ, or は/も right after the copula で."""
    if j >= 0 and morphs[j].surface in ("は", "も") and morphs[j].pos[0] == "助詞":
        j -= 1
    return j >= 0 and morphs[j].pos[0] == "助動詞" and morphs[j].lemma == "だ"


def policy_use(morphs: list, i: int) -> Optional[str]:
    """The policy class of morphs[i] when the policy writes it in kana there, else None."""
    m = morphs[i]
    prev = morphs[i - 1] if i else None
    if m.norm in PATTERNS and _te_before(morphs, i, PATTERNS[m.norm]):
        if m.norm != "行く" or m.lemma == "いける":
            return "て-pattern"
    if m.norm in HELPER_NORMS and _te_before(morphs, i, {""}):
        return "て-helper" if m.pos[:2] == ("動詞", "非自立可能") else "check"
    if m.norm == "有る" and _copula_de(morphs, i - 1):
        return "である"
    if m.norm in ("無い", "ない") and prev is not None:
        if prev.pos[0] in ("形容詞", "助動詞") and prev.surface.endswith("く"):
            return "negation ない"
        if _copula_de(morphs, i - 1) or (
            prev.pos[0] == "動詞" and prev.pos[5].startswith("未然形")
        ):
            return "negation ない"
    return None


@dataclass
class Tok:
    row: int
    morph: object
    kind: str  # kanjified | kanji | kana | other
    kanji: str = ""  # the kanji the label writes it with
    cut: str = ""  # its furigana text, spaces and tags dropped: 此[こ]の
    k: Optional[int] = None  # span number, when every group it has is addressable in one span
    groups: set = field(default_factory=set)  # its groups' order in that span
    field_range: Optional[tuple[int, int]] = None  # label offsets, when free of <b> tags
    lead: str = ""
    policy: Optional[str] = None
    context: str = ""


def _b_free_offsets(label: str) -> list[int]:
    """Label offset of each char of the label without <b> tags (plus the end)."""
    out, pos = [], 0
    for m in B_TAG_RE.finditer(label):
        out.extend(range(pos, m.start()))
        pos = m.end()
    out.extend(range(pos, len(label) + 1))
    return out


def analyze_row(row: int, label: str, tokenize=None) -> list[Tok]:
    tokenize = tokenize or generator.tokenize
    tm = text_map.build(label)
    to_label = _b_free_offsets(label)
    group_at = {}  # label offset of a group (its lead space) -> (span number, group order)
    for k, span in enumerate(note_edits.k_spans(label)):
        if span.content is not None:
            base = span.start + len("<k>")
            for gi, m in enumerate(note_edits.GROUP_RE.finditer(span.content)):
                group_at[base + m.start()] = (k, gi)
    morphs = tokenize(tm.natural)
    toks = []
    for i, m in enumerate(morphs):
        segs = tm.segs_of(m.start, m.end)
        k_segs = [s for s in segs if s.kind == "furi" and s.in_k]
        if k_segs:
            kind = "kanjified"
        elif any(s.kind == "furi" for s in segs):
            kind = "kanji"
        else:
            kind = "kana" if KANA_RE.search(m.surface) else "other"
        tok = Tok(row, m, kind, policy=policy_use(morphs, i))
        lo, hi = max(0, m.start - 10), min(len(tm.natural), m.end + 10)
        tok.context = f"{tm.natural[lo:m.start]}【{m.surface}】{tm.natural[m.end:hi]}"
        if kind == "kanjified":
            piece = TAG_RE.sub("", tm.raw_piece(m.start, m.end))
            tok.kanji = "".join(KANJI_RE.findall(re.sub(r"\[[^\]]*\]", "", piece)))
            tok.cut = piece.replace(" ", "")
            whole = (
                segs[0].nat_start == m.start and segs[-1].nat_start + len(segs[-1].natural) == m.end
            )
            places = {group_at.get(to_label[s.raw_start]) for s in k_segs}
            spans = {p[0] for p in places if p is not None}
            if whole and None not in places and len(spans) == 1:
                tok.k = spans.pop()
                tok.groups = {p[1] for p in places}
                start, end = to_label[segs[0].raw_start], to_label[segs[-1].raw_end - 1] + 1
                if end - start == segs[-1].raw_end - segs[0].raw_start:
                    tok.field_range = (start, end)
                    tok.lead = segs[0].lead
        toks.append(tok)
    return toks


def word_key(tok: Tok) -> tuple[str, str]:
    return tok.morph.norm, tok.morph.pos[0]


def format_targets(toks: list[Tok]) -> dict[tuple, tuple[Counter, Optional[str]]]:
    """Per (word, surface, kanji) cut more than one way: the cuts and the most common one, None
    on a tie or when it has more than one furigana group."""
    cuts: dict[tuple, Counter] = defaultdict(Counter)
    for t in toks:
        if t.kind == "kanjified" and t.kanji:
            cuts[(word_key(t), t.morph.surface, t.kanji)][t.cut] += 1
    out = {}
    for key, c in cuts.items():
        if len(c) > 1:
            (top, n), (_, n2) = c.most_common(2)
            out[key] = (c, top if n > n2 and top.count("[") == 1 else None)
    return out


def row_fix(label: str, toks: list[Tok], targets: dict) -> tuple[str, list[dict]]:
    """The label with its policy and format fixes, and what they were."""
    by_span: dict[int, dict] = defaultdict(lambda: {"kana": set(), "cuts": []})
    fixes = []
    for t in toks:
        if t.kind != "kanjified" or t.k is None:
            continue
        if t.policy and t.policy != "check":
            by_span[t.k]["kana"] |= t.groups
            fixes.append({"class": t.policy, "k": t.k, "word": t.cut})
            continue
        target = targets.get((word_key(t), t.morph.surface, t.kanji), (None, None))[1]
        if target and target != t.cut and t.field_range:
            by_span[t.k]["cuts"].append((t.field_range, t.lead + target))
            fixes.append({"class": "format", "k": t.k, "word": t.cut, "to": target})
    spans = note_edits.k_spans(label)
    out = label
    for k in sorted(by_span, reverse=True):
        span = spans[k]
        content_start = span.start + len("<k>")
        content = span.content
        for (a, b), new in sorted(by_span[k]["cuts"], reverse=True):
            content = content[: a - content_start] + new + content[b - content_start :]
        if by_span[k]["kana"]:
            new_span = note_edits.span_kana(content, by_span[k]["kana"])
        else:
            new_span = f"<k>{content}</k>"
        out = out[: span.start] + new_span + out[span.end :]
    return out, fixes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=Path, help="a kanjify export jsonl")
    parser.add_argument("-n", type=int, default=0, help="stop after COUNT rows")
    parser.add_argument("--out", type=Path, default=OUTPUT / "kanjify_audit_report.txt")
    parser.add_argument("--fixes", type=Path, default=OUTPUT / "kanjify_audit_fixes.jsonl")
    args = parser.parse_args()
    rows, source = read_rows(args.rows)
    rows = rows[: args.n or None]
    all_toks = [analyze_row(i, row.label) for i, row in enumerate(rows)]
    flat = [t for toks in all_toks for t in toks]
    targets = format_targets(flat)

    def where(t: Tok) -> str:
        nids = ",".join(map(str, rows[t.row].nids)) or "-"
        return f"[{t.row}] nid {nids}  {t.context}"

    lines = [f"{len(rows)} rows from {source}"]
    summary = []

    policy = defaultdict(list)
    for t in flat:
        if t.kind == "kanjified" and t.policy:
            policy[t.policy].append(t)
    for name in POLICY_CLASSES:
        hits = policy.get(name, [])
        summary.append(f"policy {name}: {len(hits)}")
        lines += ["", f"== policy / {name}: {len(hits)}"]
        for t in hits:
            fix = "" if t.k is not None else "  (no fix: context or unaddressable)"
            lines.append(f"  {t.cut}  {where(t)}{fix}")

    kanjified, kana = defaultdict(list), defaultdict(list)
    for t in flat:
        if t.kind == "kanjified" and not t.policy:
            kanjified[word_key(t)].append(t)
        elif t.kind == "kana" and not t.policy:
            kana[word_key(t)].append(t)
    left = sorted(
        (k for k in kanjified if k in kana), key=lambda k: -min(len(kanjified[k]), len(kana[k]))
    )
    summary.append(f"left-kana words: {len(left)} ({sum(len(kana[k]) for k in left)} uses)")
    lines += ["", f"== left-kana: {len(left)} words kanjified in some rows, kana in others"]
    for key in left:
        spelled = Counter(t.kanji for t in kanjified[key])
        lines.append(
            f"  {key[0]} ({key[1]}): kanjified {len(kanjified[key])} "
            f"({', '.join(f'{s} {n}' for s, n in spelled.most_common())}), kana {len(kana[key])}"
        )
        lines += [f"      kana {where(t)}" for t in kana[key][:5]]

    meaning = []
    for key, ts in kanjified.items():
        spelled = Counter(t.kanji for t in ts)
        if len(spelled) > 1:
            meaning.append((key, spelled, ts))
    meaning.sort(key=lambda m: -sum(m[1].values()))
    summary.append(f"spelling meaning: {len(meaning)} words")
    lines += ["", f"== spelling / meaning: {len(meaning)} words kanjified with different kanji"]
    for key, spelled, ts in meaning:
        lines.append(
            f"  {key[0]} ({key[1]}): " + ", ".join(f"{s} {n}" for s, n in spelled.most_common())
        )
        for s in spelled:
            lines += [f"      {s}  {where(t)}" for t in ts if t.kanji == s][:3]

    summary.append(f"spelling format: {len(targets)} words")
    lines += ["", f"== spelling / format: {len(targets)} words with the furigana cut differently"]
    for (key, surface, kanji), (cuts, target) in targets.items():
        to = f" -> {target}" if target else " (tie or several groups: not fixed)"
        lines.append(
            f"  {surface} {kanji}: " + ", ".join(f"{c} {n}" for c, n in cuts.most_common()) + to
        )

    mismatched = defaultdict(list)
    for i, row in enumerate(rows):
        kind = mismatch(row.sentence, row.label)
        if kind:
            mismatched[kind].append(i)
    for kind in ("text", "furigana"):
        found = mismatched[kind]
        summary.append(f"mismatch {kind}: {len(found)} rows")
        lines += ["", f"== mismatch / {kind}: {len(found)} labels that don't reverse to the source"]
        for i in found:
            nids = ",".join(map(str, rows[i].nids)) or "-"
            lines += [
                f"  [{i}] nid {nids}",
                f"      source {rows[i].sentence}",
                f"      label  {rows[i].label}",
            ]

    fixed = 0
    with open(args.fixes, "w", encoding="utf-8") as f:
        for i, (row, toks) in enumerate(zip(rows, all_toks)):
            after, fixes = row_fix(row.label, toks, targets)
            if fixes and after != row.label:
                fixed += 1
                out = {"row": i, "nids": row.nids, "before": row.label, "after": after}
                f.write(json.dumps({**out, "fixes": fixes}, ensure_ascii=False) + "\n")
    summary.append(f"fix rows: {fixed} -> {args.fixes.name}")

    args.out.write_text("\n".join(summary + [""] + lines) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
