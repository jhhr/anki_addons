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


BASE_RE = re.compile(r"(?:^|(?<=[\s>\]]))([^\s<>\[\]]+)\[([^\]]*)\]")
NOT_KANJI_RE = re.compile(r"^[^一-龯㐀-䶿々〆0-9０-９]+")
BR_RE = re.compile(r"<br\s*/?>")


def furigana_suspects(sentence: str) -> list[str]:
    """What a program can see is wrong with a furigana sentence, without reading it: a group
    written with no space after kana (`つ買[か]`: Anki's furigana filter puts か over つ買, so the
    reading covers kana it doesn't read), a group with no kanji or no reading, and a kanji with
    no reading at all. The step 2 agents check what takes reading (a reading wrong for its
    kanji or its context); these they are told to leave to this."""
    text = BR_RE.sub(" ", sentence)
    out = []
    for m in BASE_RE.finditer(text):
        base, reading = m[1], m[2]
        lead = NOT_KANJI_RE.match(base)
        if lead and lead[0].strip("ヶヵ"):
            out.append(f"no space before the group: {m[0]}")
        elif not reading:
            out.append(f"no reading: {m[0]}")
    rest = TAG_RE.sub("", BASE_RE.sub("", text))
    bare = re.findall(r"[一-龯㐀-䶿]+", rest)
    if bare:
        out.append("kanji with no reading: " + ", ".join(bare))
    return out


def relative(path: Path) -> str:
    """A path as files and logs record it: from the golden set's directory or the addon root
    when under one, so nothing written names this machine's folders."""
    for base in (GOLDEN, ADDON_ROOT):
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return path.name
