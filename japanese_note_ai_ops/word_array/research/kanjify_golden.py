"""What the kanjify golden set's scripts share: where its files are, its rows and the check a
row must pass before it is accepted.

The golden set is every note sentence of the collection kanjified by agents that follow one
written policy (`kanjify_agents/policy.md`), for scoring kanjify_sentence (`kanjify_eval.py
--rows`). It is built from files only, never from a running Anki, so that its workers also run
in cloud sessions: the dump is the one step that asks AnkiConnect.

    kanjify_golden_dump.py       every note's sentence fields -> dump.jsonl
    kanjify_golden_inventory.py  dump -> the step 1 words and step 2 sentences
    kanjify_golden_render.py     one prompt per word or batch -> queues/<name>.jsonl
    agent_queue.py               runs a queue through `claude -p`, one result file per item
    kanjify_golden_collate.py    results -> accepted and rejected rows, the word queue, the
                                 policy questions, the calibration report

Everything it writes holds the collection's sentences, so it lives in the private test data
checkout (`evals/kanjify_golden/`, `_bootstrap.eval_file`), never in this repo.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

from _bootstrap import ADDON_ROOT, eval_file

GOLDEN = eval_file("kanjify_golden")
DUMP = GOLDEN / "dump.jsonl"
INVENTORY = GOLDEN / "inventory"
QUEUES = GOLDEN / "queues"
RESULTS = GOLDEN / "results"
COLLATED = GOLDEN / "collated"
AGENTS_DIR = Path(__file__).resolve().parent / "kanjify_agents"
POLICY = AGENTS_DIR / "policy.md"
# Written by the collate step: the step 1 decisions as step 2 prompts read them
DECISIONS = GOLDEN / "decisions.jsonl"
# Every word step 2 handed back that the inventory has no word for, with its sentences, over
# every round: collate adds each round's, step 1 decides them, and step 2 finds a decision for
# a sentence through this file, since the inventory never lists these words
HANDED = GOLDEN / "handed_back.jsonl"
# The hand-fixed labels, which step 2 labels again unknowingly for the calibration report
HAND = eval_file("kanjify_sentence_data.jsonl")

K_SPAN_RE = re.compile(r"<k>(.*?)</k>", re.S)
GROUP_RE = re.compile(r"[\d々ヶヵ〆一-龯㐀-䶿]+\[[^\]]*\]")
VERSION_RE = re.compile(r"^Policy version: (\S+)", re.M)
B_TAG_RE = re.compile(r"</?b>")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s")


class Sentence(NamedTuple):
    sid: str  # sentence_id() of `sentence`
    sentence: str  # the op's input: the furigana sentence field
    nids: list[int]  # every note holding it


def sentence_id(sentence: str) -> str:
    """A short stable id for one input sentence: the dump groups notes by it, and prompts,
    results and the collated rows name the sentence by it."""
    return "s" + hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:10]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_sentences(dump: Path = DUMP) -> list[Sentence]:
    """The dump's sentences, notes sharing one sentence as one row."""
    by_sentence: dict[str, list[int]] = {}
    for note in read_jsonl(dump):
        if note["sentence"]:
            by_sentence.setdefault(note["sentence"], []).append(note["nid"])
    return [Sentence(sentence_id(s), s, sorted(nids)) for s, nids in by_sentence.items()]


def translations(dump: Path = DUMP) -> dict[str, str]:
    """sid -> the note's own translation of the sentence, the first of its notes' that has one.
    The prompts show it: the sentence alone sometimes doesn't say which word it means (カキ,
    oyster or persimmon)."""
    out: dict[str, str] = {}
    for note in read_jsonl(dump):
        if note["sentence"] and note.get("translation"):
            out.setdefault(sentence_id(note["sentence"]), note["translation"])
    return out


def hand_labels(sentences: list[Sentence]) -> dict[str, dict]:
    """sid -> the hand-fixed row of that sentence, matched by note id. A row whose sentence
    (without `<b>`, which the export kept) is no longer the note's input is left out: it labels
    a sentence that isn't in the dump."""
    by_nid = {nid: s for s in sentences for nid in s.nids}
    out = {}
    for row in read_jsonl(HAND):
        for nid in row.get("nids") or []:
            s = by_nid.get(nid)
            if s is None:
                continue
            same = WS_RE.sub("", B_TAG_RE.sub("", row["sentence"])) == WS_RE.sub(
                "", B_TAG_RE.sub("", s.sentence)
            )
            if same:
                out[s.sid] = {"kanjified": B_TAG_RE.sub("", row["kanjified"]), "nids": row["nids"]}
    return out


def policy_text() -> str:
    return POLICY.read_text(encoding="utf-8")


def policy_version(text: Optional[str] = None) -> str:
    """The version line of policy.md plus a hash of its text: a result made before an edit
    that forgot to bump the line is still told apart."""
    text = policy_text() if text is None else text
    m = VERSION_RE.search(text)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{m[1] if m else '?'}+{digest}"


def decisions_version(path: Path = DECISIONS) -> str:
    if not path.exists():
        return "none"
    return hashlib.sha1(path.read_bytes()).hexdigest()[:8]


def row_problem(sentence: str, kanjified: str) -> Optional[str]:
    """Why a labelled row can't be accepted, or None. The label must read as the input and only
    kanjify: `kanjify_note.edit_problem` (the same reading, `<k>` tags that pair up), the op's
    reverse check (`kanjify_audit.mismatch`: every span back in kana is the input, so nothing
    outside a span changed), and every span holding a furigana group. An unchanged sentence is a
    valid label, unlike an unchanged note edit."""
    import kanjify_audit
    import kanjify_note

    problem = kanjify_note.edit_problem(sentence, kanjified)
    if problem and problem != "nothing changed":
        return problem
    kind = kanjify_audit.mismatch(sentence, kanjified)
    if kind == "furigana":
        return "outside its <k> spans the text differs from the input's (a group regrouped, or a"\
            " word kanjified without <k>, or <k> around a word already in kanji)"
    if kind == "text":
        return "with every <k> span back in kana it isn't the input sentence"
    for m in K_SPAN_RE.finditer(kanjified):
        if not GROUP_RE.search(m[1]):
            return f"the span <k>{m[1]}</k> holds no kanji[reading] group"
    return None


def hiragana(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


BASE_RE = re.compile(r"(?:^|(?<=[\s>\]]))([^\s<>\[\]]+)\[([^\]]*)\]")
NOT_KANJI_RE = re.compile(r"^[^一-龯㐀-䶿々〆0-9０-９]+")
KANA_ONLY_RE = re.compile(r"[ぁ-ゖァ-ヺーゝゞヽヾ]+")
KATAKANA_RE = re.compile(r"[ァ-ヺー]+")
BR_RE = re.compile(r"<br\s*/?>")


def kana_lead(base: str, reading: str) -> tuple[str, bool]:
    """The kana a group's base starts with before its kanji ("" for none; a counter's ヶ / ヵ
    is not kana here), and whether the reading covers it: `ネコ科[ねこか]` is katakana written
    inside a kanji word's group, `つ買[か]` a group with no space after the kana before it.
    Hiragana never counts as covered: in `は鋼[はがね]` or `い命[いのち]` the particle or
    okurigana before the kanji only happens to be the first kana of the kanji's reading."""
    lead = NOT_KANJI_RE.match(base)
    kana = lead[0] if lead and lead[0].strip("ヶヵ") else ""
    covered = hiragana(reading).startswith(hiragana(kana)) and len(reading) > len(kana)
    return kana, bool(KATAKANA_RE.fullmatch(kana)) and covered


def kana_in_group(sentence: str) -> bool:
    """Whether the input writes kana inside a kanji word's furigana group, the reading covering
    it (`ウシ科[うしか]`, `カ国[かこく]`). The user counts that as broken furigana, fixed in the
    note by splitting the group (`ウシ 科[か]`): the sentence is kanjified only from the fixed
    note, since a label of the broken one would leave the kana word inside the group."""
    return any(kana_lead(m[1], m[2])[1] for m in BASE_RE.finditer(BR_RE.sub(" ", sentence)))


def furigana_suspects(sentence: str) -> list[str]:
    """What a program can see is wrong with a furigana sentence, without reading it: a group
    written with no space after kana (`つ買[か]`: Anki's furigana filter puts か over つ買, so the
    reading covers kana it doesn't read), kana written inside a kanji word's group
    (`ウシ科[うしか]`), a group with no kanji or no reading, and a kanji with no reading at all.
    The step 2 agents check what takes reading (a reading wrong for its kanji or its context);
    these they are told to leave to this."""
    text = BR_RE.sub(" ", sentence)
    out = []
    for m in BASE_RE.finditer(text):
        base, reading = m[1], m[2]
        kana, covered = kana_lead(base, reading)
        if covered:
            out.append(f"kana inside the group: {m[0]}")
        elif kana:
            out.append(f"no space before the group: {m[0]}")
        elif not reading:
            out.append(f"no reading: {m[0]}")
        elif not KANA_ONLY_RE.fullmatch(reading):
            # 額[がく, ひたい]: a dictionary's readings pasted whole, not the one read here
            out.append(f"a reading that isn't one kana word: {m[0]}")
    rest = TAG_RE.sub("", BASE_RE.sub("", text))
    bare = re.findall(r"[一-龯㐀-䶿]+", rest)
    if bare:
        out.append("kanji with no reading: " + ", ".join(bare))
    return out


def fix_groups(sentence: str) -> str:
    """The sentence with the two repairs a program can make to a group's kana lead: a space
    before a group written right after kana (`つ買[か]` -> `つ 買[か]`), and kana inside a kanji
    word's group split out with its part of the reading (`ウシ科[うしか]` -> `ウシ 科[か]`)."""

    def fix(m: re.Match) -> str:
        kana, covered = kana_lead(m[1], m[2])
        if not kana:
            return m[0]
        reading = m[2][len(kana):] if covered else m[2]
        return f"{kana} {m[1][len(kana):]}[{reading}]"

    return BASE_RE.sub(fix, sentence)


# The kinds of furigana_suspects that fix_groups repairs
REPAIRED = ("no space before the group", "kana inside the group")
READING_RE = re.compile(r"\[[^\]]*\]")


def plain_text(sentence: str) -> str:
    """The sentence with no readings and no whitespace: what a furigana fix leaves alone."""
    return WS_RE.sub("", READING_RE.sub("", sentence))


def relative(path: Path) -> str:
    """A path as files and logs record it: from the golden set's directory or the addon root
    when under one, so nothing written names this machine's folders."""
    for base in (GOLDEN, ADDON_ROOT):
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return path.name
