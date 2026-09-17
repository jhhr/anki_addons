"""Judging array words by hand, in the browser, for the judge's eval set.

    py -3.10 word_array/research/hand_judge.py [--corpus export] [--per-word 1] [--port 8790]

Serves a page on localhost that shows one word at a time from sentences of the migration export,
taken in random order and generated on the spot: the sentence with the word marked (the words it
is a component of underlined), its dictionary form, reading and part of speech, the words it is
part of and made of, and the judge's rules for its group. Match / Don't match go into
`output/word_matching_judge_hand_labels.jsonl` (`hand_labels.py`), which `judge_eval.py build`
lays over the checked export's labels. Only the groups ticked on the page are offered, and a word
already judged `--per-word` times in the same group and parent is not offered again, so a few
hundred judgements cover many different words. Skip is not saved; Undo takes back the last one.
Under the buttons each group shows labelled/total, the total growing (marked +) while a thread
generates the rest of the corpus.

Open in Anki shows the sentence's notes (the export's `nids`) in Anki's browser, and Refetch note
reads them back after an edit there, both through AnkiConnect (`--anki-connect`), which this server
calls so that a phone can use them too. A changed sentence is generated again at the front of the
queue, its labels move to the new text where the new array still has their word (the rest are
dropped), and its rows in the export and the checked subset take the new text.

The sentence outlines its `<k>` spans: clicking one, once confirmed, writes the span as kana to
every note of the sentence (`note_edits.unkanjify`) and goes on as a refetch does. Anki can't undo
that write, so Revert last edit writes the old field back while Anki still has the edited one.
"""

import argparse
import html
import json
import random
import re
import sys
import threading
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import NamedTuple, Optional
from urllib.parse import urlparse

import anki_connect
import hand_labels
import migrate_fit
import note_edits
from _bootstrap import load

judge_rules = load("judge")
match_flags = load("match_flags")

DEFAULT_GROUPS = (
    "noun-sub",
    "noun-phrase",
    "prefix-verb",
    "suffix-verb",
    "prefix",
    "suffix",
    "counter",
)
GROUPS = [g for g in judge_rules.POS_RULES]
MORE_SENTENCES = 500  # generated per request at most, looking for a word to offer

TAG_RE = re.compile(r"<[^>]+>")
RUBY_RE = re.compile(r"(?<![^ \]>])([^ \[\]<>]+?)\[([^\]]*)\]")


class Candidate(NamedTuple):
    sentence: str
    arr: list
    placed: hand_labels.Placed
    group: str


def word_key(group: str, path: tuple[str, ...], reading: str) -> tuple:
    """What --per-word counts: a word in one group under one parent."""
    return (group, tuple(path[-2:]), reading)


K_TAG_RE = re.compile(r"(</?k>)")


def _ruby(text: str) -> str:
    text = html.unescape(TAG_RE.sub("", text))
    out, pos = [], 0
    for m in RUBY_RE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        out.append(f"<ruby>{html.escape(m[1])}<rt>{html.escape(m[2])}</rt></ruby>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out).replace(" ", "")


def ruby(raw_text: str, k: Optional[dict] = None) -> str:
    """Raw text as ruby, the text inside a `<k>` span wrapped in a span carrying its number.
    `k` keeps count across the raw texts of one sentence: which span is open, and how many
    were opened."""
    k = {"open": None, "count": 0} if k is None else k
    out = []
    for chunk in K_TAG_RE.split(raw_text):
        if chunk == "<k>":
            k["open"] = k["count"]
            k["count"] += 1
        elif chunk == "</k>":
            k["open"] = None
        elif chunk:
            text = _ruby(chunk)
            if text and k["open"] is not None:
                text = f'<span class="k" data-k="{k["open"]}">{text}</span>'
            out.append(text)
    return "".join(out)


def render(items: list, target: list, parents: list[list], k: Optional[dict] = None) -> str:
    k = {"open": None, "count": 0} if k is None else k
    parts = []
    for elem in items:
        if elem is target:
            parts.append(f"<mark>{ruby(elem[0], k)}</mark>")
        elif len(elem) > 1 and any(elem is p for p in parents):
            parts.append(f'<span class="parent">{render(elem[5], target, parents, k)}</span>')
        else:
            parts.append(ruby(elem[0], k))
    return "".join(parts)


def describe(elem: list) -> str:
    return f"{elem[2]} [{elem[3]}], {elem[1]}"


NO_NIDS = (
    "No note ids for this sentence: the export predates them. Run Tools > AI ops: generate test"
    " data in Anki and restart this page's server to open or refetch it."
)
NO_CHANGE = (
    "No change in Anki. The editor saves a field when it loses focus: click out of it, then"
    " refetch."
)
REVERTED = "Reverted."


class Refused(Exception):
    """An edit not made, with why, for the page."""


class Edit(NamedTuple):
    nid: int
    field: str
    old: str
    new: str


def _stripped(raw: str) -> str:
    return migrate_fit.html_stripping.strip_context_sentences(raw)


class Session:
    def __init__(
        self,
        sentences: list[str],
        lexicon: dict,
        labels_path,
        per_word: int,
        nids: Optional[dict[str, list[int]]] = None,
        raws: Optional[dict[int, str]] = None,
        generate=None,
    ):
        self.sentences = sentences
        self.lexicon = lexicon
        self.next_sentence = 0
        self.labels_path = labels_path
        self.per_word = per_word
        self.labels = hand_labels.read_labels(labels_path)
        self.pool: list[Candidate] = []
        self.history: list[tuple[Candidate, str]] = []
        self._count_labels()
        self.offered: dict[int, Candidate] = {}
        self.judged_now = 0
        # Words of every sentence generated so far, for each group's total
        self.placements: Counter[tuple] = Counter()
        self.arrays: dict[str, list] = {}
        # Context-stripped sentence → its notes, and each note's raw sentence field
        self.nids = nids or {}
        self.raws = raws or {}
        self.generate = generate or migrate_fit.generator.generate
        # Writes to Anki, latest last: the notes of one un-kanjified span each
        self.edits: list[list[Edit]] = []
        self.lock = threading.Lock()

    def _count_labels(self) -> None:
        skipped = {
            hand_labels.placed_key(c.sentence, c.placed) for c, l in self.history if l == "skip"
        }
        self.done: set[tuple] = {hand_labels.label_key(row) for row in self.labels} | skipped
        self.word_counts: Counter[tuple] = Counter(
            word_key(row["group"], tuple(row["path"]), row["reading"]) for row in self.labels
        )

    def generate_all(self) -> None:
        """Generates the sentences no request has reached yet, so the group totals cover the
        whole corpus; run in a thread, as it takes about a minute for the export."""
        while True:
            with self.lock:
                if not self._generate_more():
                    return

    def _generate_more(self) -> bool:
        if self.next_sentence >= len(self.sentences):
            return False
        sentence = self.sentences[self.next_sentence]
        self.next_sentence += 1
        self.pool.extend(self._generate(sentence))
        return True

    def _generate(self, sentence: str) -> list[Candidate]:
        try:
            arr = self.generate(sentence, self.lexicon)
        except Exception as exc:  # a crash here is migrate_fit's business, not the GUI's
            print(f"generate failed: {exc!r}: {sentence}", file=sys.stderr)
            return []
        self.arrays[sentence] = arr
        out = []
        for placed in hand_labels.placed_words(arr):
            elem = placed.elem
            if elem[1] in judge_rules.AUTO_DONT_MATCH_POS:
                continue
            if match_flags.match_state(elem) != match_flags.MatchState.UNJUDGED:
                continue
            group = judge_rules.rule_group(elem, placed.parents)
            out.append(Candidate(sentence, arr, placed, group))
            self.placements[word_key(group, placed.path, elem[3])] += 1
        return out

    def _forget(self, sentence: str) -> None:
        """Takes a sentence out of the corpus, with every word offered or counted from it."""
        if sentence in self.sentences:
            i = self.sentences.index(sentence)
            del self.sentences[i]
            if i < self.next_sentence:
                self.next_sentence -= 1
        for cand in self.pool:
            if cand.sentence == sentence:
                self.placements[word_key(cand.group, cand.placed.path, cand.placed.elem[3])] -= 1
        self.placements = +self.placements
        self.pool = [cand for cand in self.pool if cand.sentence != sentence]
        self.offered = {k: cand for k, cand in self.offered.items() if cand.sentence != sentence}
        self.arrays.pop(sentence, None)

    def replace_sentence(self, old: str, new_texts: list[str]) -> tuple[int, int]:
        """A sentence edited in Anki: its notes now read `new_texts`, first note's text first
        (`old` among them where some notes kept it). The new texts are generated at the front of
        the pool, so the word being judged comes back next, and the labels of `old` move to the
        first text where its array still has their word. Returns labels moved and dropped."""
        with self.lock:
            if old not in new_texts:
                self._forget(old)
            for text in reversed([t for t in dict.fromkeys(new_texts) if t != old]):
                self._forget(text)
                self.sentences.insert(0, text)
                self.next_sentence += 1
                self.pool[0:0] = self._generate(text)
            moved = dropped = 0
            new = new_texts[0]
            if new != old and new in self.arrays:
                self.labels, moved, dropped = hand_labels.move_labels(
                    self.labels, old, new, self.arrays[new]
                )
                self._save()
            self.history = [(c, l) for c, l in self.history if c.sentence != old]
            self._count_labels()
            return moved, dropped

    def apply_notes(self, sentence: str, raws: dict[int, str], paths) -> str:
        """Takes the notes of `sentence` as Anki has them now (raw field by note id, in the
        session's note order): unchanged, or the sentence replaced and the jsonl files at
        `paths` rewritten. Returns what happened, for the page."""
        texts = {nid: _stripped(raw) for nid, raw in raws.items()}
        new_texts = list(dict.fromkeys(t for t in texts.values() if t.strip()))
        if not new_texts or new_texts == [sentence]:
            return NO_CHANGE
        moved, dropped = self.replace_sentence(sentence, new_texts)
        new_raw = {nid: raw for nid, raw in raws.items() if raw != self.raws.get(nid)}
        renamed: dict[str, str] = {}
        for nid, raw in new_raw.items():
            if nid in self.raws:
                renamed.setdefault(self.raws[nid], raw)
        rows = sum(hand_labels.rewrite_rows(path, new_raw, renamed) for path in paths)
        with self.lock:
            self.nids.pop(sentence, None)
            for nid, text in texts.items():
                ids = self.nids.setdefault(text, [])
                if nid not in ids:
                    ids.append(nid)
            self.raws.update(raws)
        shown = " / ".join(new_texts)
        return (
            f"Sentence now {shown}: {moved} labels moved, {dropped} dropped,"
            f" {rows} jsonl rows rewritten."
        )

    def refetch(self, sentence: str, anki, config: dict, paths) -> str:
        nids = self.nids.get(sentence)
        if not nids:
            return NO_NIDS
        raws = {
            info["noteId"]: anki_connect.note_sentence(config, info)
            for info in anki.notes_info(nids)
            if info
        }
        return self.apply_notes(sentence, raws, paths)

    def browse(self, sentence: str, anki) -> str:
        nids = self.nids.get(sentence)
        if not nids:
            return NO_NIDS
        anki.gui_browse(anki_connect.nids_query(nids))
        return f"Opened {len(nids)} note(s) in Anki's browser."

    def unkanjify(self, sentence: str, k: int, anki, config: dict, paths) -> str:
        """Turns the `k`-th `<k>` span of `sentence` into kana in every note of it, as Anki has
        them now; refused, nothing written, when a note reads otherwise or the span can't be
        edited."""
        nids = self.nids.get(sentence)
        if not nids:
            raise Refused(NO_NIDS)
        edits = []
        for info in anki.notes_info(nids):
            if not info:
                continue
            raw = anki_connect.note_sentence(config, info)
            if _stripped(raw) != sentence:
                raise Refused(
                    f"Note {info['noteId']} reads otherwise in Anki now: refetch it first."
                )
            try:
                new = note_edits.unkanjify(raw, k)
            except note_edits.NoteEditError as e:
                raise Refused(str(e)) from None
            field = anki_connect.sentence_field(config, info.get("modelName", ""))
            edits.append(Edit(info["noteId"], field, raw, new))
        if not edits:
            raise Refused("None of the sentence's notes is in Anki any more.")
        written = self._write(anki, edits, "new")
        self.edits.append(written)
        message = self.apply_notes(sentence, {e.nid: e.new for e in written}, paths)
        if len(written) < len(edits):
            message = f"Only {len(written)} of {len(edits)} notes written. {message}"
        return message

    def revert(self, anki, config: dict, paths) -> str:
        """Writes back the fields of the last un-kanjified span, if Anki still has what was
        written."""
        if not self.edits:
            raise Refused("No edit to revert.")
        edits = self.edits[-1]
        infos = {info["noteId"]: info for info in anki.notes_info([e.nid for e in edits]) if info}
        for e in edits:
            value = infos.get(e.nid, {}).get("fields", {}).get(e.field, {}).get("value")
            if value != e.new:
                raise Refused(f"Note {e.nid} changed in Anki since the edit: not reverted.")
        written = self._write(anki, edits, "old")
        self.edits.pop()
        message = self.apply_notes(_stripped(edits[0].new), {e.nid: e.old for e in written}, paths)
        return f"{REVERTED} {message}"

    @staticmethod
    def _write(anki, edits: list[Edit], side: str) -> list[Edit]:
        """Writes each edit's `side` value; the edits written. A failure after the first write
        keeps those written, as Anki already has them."""
        written: list[Edit] = []
        for e in edits:
            try:
                anki.update_note_fields(e.nid, {e.field: getattr(e, side)})
            except anki_connect.AnkiConnectError:
                if not written:
                    raise
                break
            written.append(e)
        return written

    def group_totals(self) -> Counter[str]:
        """How many words of each group can be labelled, at most --per-word of each word."""
        totals: Counter[str] = Counter()
        for key, n in self.placements.items():
            totals[key[0]] += min(n, self.per_word)
        return totals

    def _offerable(self, cand: Candidate, groups: set[str]) -> bool:
        p = cand.placed
        return (
            cand.group in groups
            and hand_labels.placed_key(cand.sentence, p) not in self.done
            and self.word_counts[word_key(cand.group, p.path, p.elem[3])] < self.per_word
        )

    def next_candidate(self, groups: set[str]) -> Optional[Candidate]:
        with self.lock:
            scanned = 0
            while True:
                for cand in self.pool[scanned:]:
                    if self._offerable(cand, groups):
                        return cand
                scanned = len(self.pool)
                for _ in range(MORE_SENTENCES):
                    if not self._generate_more():
                        return None
                    if len(self.pool) > scanned:
                        break
                else:
                    return None

    def item(self, cand: Optional[Candidate]) -> dict:
        with self.lock:
            totals = self.group_totals()
            generated = self.next_sentence
        labelled = Counter(row["group"] for row in self.labels)
        stats = {
            "labels": len(self.labels),
            "now": self.judged_now,
            "by_group": [
                [group, labelled[group], totals[group]]
                for group, _ in (labelled + totals).most_common()
            ],
            "sentences": f"{generated}/{len(self.sentences)}",
            "counting": generated < len(self.sentences),
            "can_revert": bool(self.edits),
        }
        if cand is None:
            return {"item": None, "stats": stats}
        item_id = id(cand)
        self.offered[item_id] = cand
        p = cand.placed
        subs = [s for s in p.elem[5] if len(s) > 1]
        return {
            "item": {
                "id": item_id,
                "html": render(cand.arr, p.elem, p.parents),
                "word": describe(p.elem),
                "group": cand.group,
                "part_of": [describe(parent) for parent in reversed(p.parents)],
                "made_of": " + ".join(f"{s[2]} [{s[3]}]" for s in subs),
                "rules": judge_rules.POS_RULES[cand.group],
                "notes": len(self.nids.get(cand.sentence, [])),
                "kspans": note_edits.changes(cand.sentence),
                "no_notes": NO_NIDS,
            },
            "stats": stats,
        }

    def _save(self) -> None:
        hand_labels.write_labels(self.labels, self.labels_path)

    def judge(self, item_id: int, label: str) -> None:
        cand = self.offered.pop(item_id, None)
        if cand is None:
            return
        p = cand.placed
        key = hand_labels.placed_key(cand.sentence, p)
        self.done.add(key)
        self.history.append((cand, label))
        if label == "skip":
            return
        self.labels = [row for row in self.labels if hand_labels.label_key(row) != key]
        self.labels.append(hand_labels.make_label(cand.sentence, p, cand.group, label))
        self.word_counts[word_key(cand.group, p.path, p.elem[3])] += 1
        self.judged_now += 1
        self._save()

    def undo(self) -> Optional[Candidate]:
        if not self.history:
            return None
        cand, label = self.history.pop()
        p = cand.placed
        key = hand_labels.placed_key(cand.sentence, p)
        self.done.discard(key)
        if label != "skip":
            self.labels = [row for row in self.labels if hand_labels.label_key(row) != key]
            self.word_counts[word_key(cand.group, p.path, p.elem[3])] -= 1
            self.judged_now -= 1
            self._save()
        return cand


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hand judge</title>
<style>
:root { --bg:#f7f7f5; --fg:#1d1d1f; --muted:#6b6b70; --card:#fff; --line:#dcdcd8;
  --mark:#ffe28a; --parent:#3b7ddd; --match:#2e8b57; --dont:#c0392b; }
@media (prefers-color-scheme: dark) { :root { --bg:#18181a; --fg:#ececec; --muted:#9a9aa0;
  --card:#232326; --line:#3a3a3e; --mark:#7a5d00; --parent:#78a9ff; } }
body { background:var(--bg); color:var(--fg); margin:0; padding:16px;
  font:15px/1.5 system-ui, "Segoe UI", "Yu Gothic UI", "Meiryo", sans-serif; }
main { max-width:760px; margin:0 auto; }
.groups { display:flex; flex-wrap:wrap; gap:4px 12px; font-size:13px; color:var(--muted); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:20px; margin:12px 0; }
.sentence { font-size:26px; line-height:2.2; }
.sentence rt { font-size:11px; color:var(--muted); }
mark { background:var(--mark); color:inherit; border-radius:3px; padding:0 2px; }
.parent { border-bottom:3px solid var(--parent); }
.word { font-size:20px; margin-top:12px; }
.meta { color:var(--muted); }
.badge { display:inline-block; font-size:12px; border:1px solid var(--line);
  border-radius:99px; padding:0 8px; margin-left:6px; color:var(--muted); }
.buttons { display:flex; flex-wrap:wrap; gap:8px; }
button { font-size:17px; padding:12px 18px; border-radius:8px; border:1px solid var(--line);
  background:var(--card); color:var(--fg); cursor:pointer; flex:1 1 120px; }
button.match { background:var(--match); color:#fff; border-color:var(--match); }
button.dont { background:var(--dont); color:#fff; border-color:var(--dont); }
details { margin-top:12px; color:var(--muted); white-space:pre-wrap; font-size:13px; }
.stats { font-size:13px; color:var(--muted); }
.notes { margin-top:8px; } .notes button { font-size:14px; padding:8px 12px; }
.message { font-size:14px; white-space:pre-wrap; }
.k { outline:1px dotted var(--muted); outline-offset:1px; border-radius:3px; cursor:pointer; }
</style></head><body><main>
<div class="groups" id="groups"></div>
<div class="card" id="card">Loading...</div>
<div class="buttons">
  <button class="match" onclick="send('match')">Match <small>(M)</small></button>
  <button class="dont" onclick="send('dontmatch')">Don't match <small>(D)</small></button>
  <button onclick="send('skip')">Skip <small>(S)</small></button>
  <button onclick="undo()">Undo <small>(U)</small></button>
</div>
<div class="buttons notes">
  <button id="browse" onclick="note('/api/browse')">Open in Anki <small>(O)</small></button>
  <button id="refetch" onclick="note('/api/refetch')">Refetch note <small>(R)</small></button>
  <button id="revert" onclick="action('/api/revert', {})" disabled>Revert last edit</button>
</div>
<p class="message" id="message"></p>
<p class="stats" id="stats"></p>
</main><script>
const GROUPS = __GROUPS__, DEFAULT = __DEFAULT__;
let chosen, current = null;
try { chosen = JSON.parse(localStorage.getItem("groups")) } catch (e) {}
if (!Array.isArray(chosen)) chosen = DEFAULT;
const box = document.getElementById("groups");
for (const g of GROUPS) {
  const l = document.createElement("label");
  l.innerHTML = `<input type="checkbox" ${chosen.includes(g) ? "checked" : ""}> ${g}`;
  l.firstChild.onchange = e => {
    chosen = e.target.checked ? [...chosen, g] : chosen.filter(x => x !== g);
    try { localStorage.setItem("groups", JSON.stringify(chosen)) } catch (e) {}
    if (current === null || !chosen.includes(current.group)) post("/api/next", {});
  };
  box.appendChild(l);
}
const esc = s => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"})[c]);
function show(data) {
  current = data.item;
  const s = data.stats;
  document.getElementById("stats").textContent =
    `${s.now} judged now, ${s.labels} saved, sentences read ${s.sentences} | ` +
    s.by_group.map(([g, n, total]) => `${g} ${n}/${total}${s.counting ? "+" : ""}`).join(", ");
  const card = document.getElementById("card");
  document.getElementById("revert").disabled = !s.can_revert;
  for (const b of ["browse", "refetch"])
    document.getElementById(b).disabled = !current || !current.notes;
  if (!current) { card.textContent = "Nothing left to offer in the ticked groups."; return; }
  if (!current.notes) document.getElementById("message").textContent = current.no_notes;
  card.innerHTML = `<div class="sentence">${current.html}</div>
    <div class="word">${esc(current.word)}<span class="badge">${esc(current.group)}</span></div>
    ${current.part_of.map(p => `<div class="meta">Part of: ${esc(p)}</div>`).join("")}
    ${current.made_of ? `<div class="meta">Made of: ${esc(current.made_of)}</div>` : ""}
    <details><summary>Judge rules for ${esc(current.group)}</summary>${esc(current.rules)}</details>`;
}
async function post(url, body) {
  const r = await fetch(url, {method: "POST", body: JSON.stringify({...body, groups: chosen})});
  show(await r.json());
}
const message = text => { document.getElementById("message").textContent = text; };
function send(label) { if (current) { message(""); post("/api/judge", {id: current.id, label}); } }
function undo() { message(""); post("/api/undo", {}); }
async function action(url, body) {
  message("...");
  const r = await fetch(url, {method: "POST", body: JSON.stringify(body)});
  const data = await r.json();
  if (data.reload) await post("/api/next", {});
  message(data.message);
}
function note(url, extra) {
  if (current && current.notes) action(url, {...extra, id: current.id});
}
document.getElementById("card").addEventListener("click", e => {
  const span = e.target.closest(".k");
  if (!span || !current) return;
  const k = Number(span.dataset.k), change = current.kspans[k];
  if (!current.notes) { message(current.no_notes); return; }
  if (!change) { message("That <k> span isn't closed or has no furigana: fix it in Anki."); return; }
  if (confirm(`${change[0]} → ${change[1]}\n\nWrite this to ${current.notes} note(s) in Anki?`))
    note("/api/unkanjify", {k});
});
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey || e.target.tagName === "INPUT") return;
  const k = e.key.toLowerCase();
  if (k === "m") send("match");
  else if (k === "d") send("dontmatch");
  else if (k === "s") send("skip");
  else if (k === "u") undo();
  else if (k === "o") note("/api/browse");
  else if (k === "r") note("/api/refetch");
});
post("/api/next", {});
</script></body></html>"""


def note_action(session: Session, path: str, body: dict, anki, config: dict, paths) -> dict:
    """`/api/browse`, `/api/refetch` and `/api/unkanjify` on the card's sentence, and
    `/api/revert`: a message for the page, and whether the card is to be reloaded because the
    sentence changed."""
    try:
        if path == "/api/revert":
            return {"message": session.revert(anki, config, paths), "reload": True}
        cand = session.offered.get(int(body.get("id", 0)))
        if cand is None:
            return {"message": "That card is gone; reload the page.", "reload": False}
        if path == "/api/browse":
            return {"message": session.browse(cand.sentence, anki), "reload": False}
        if path == "/api/unkanjify":
            k = int(body.get("k", -1))
            message = session.unkanjify(cand.sentence, k, anki, config, paths)
        else:
            message = session.refetch(cand.sentence, anki, config, paths)
    except (anki_connect.AnkiConnectError, Refused) as e:
        return {"message": str(e), "reload": False}
    return {"message": message, "reload": message not in (NO_CHANGE, NO_NIDS)}


def make_handler(session: Session, anki=None, config=None, paths=()):
    page = (
        PAGE.replace("__GROUPS__", json.dumps(GROUPS))
        .replace("__DEFAULT__", json.dumps(list(DEFAULT_GROUPS)))
        .encode("utf-8")
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path != "/":
                self.send_error(404)
                return
            self._send(page, "text/html; charset=utf-8")

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            groups = set(body.get("groups") or [])
            if path in ("/api/browse", "/api/refetch", "/api/unkanjify", "/api/revert"):
                data = note_action(session, path, body, anki, config or {}, paths)
                self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")
                return
            if path == "/api/judge" and body.get("label") in ("match", "dontmatch", "skip"):
                session.judge(int(body.get("id", 0)), body["label"])
                cand = session.next_candidate(groups)
            elif path == "/api/undo":
                cand = session.undo() or session.next_candidate(groups)
            elif path == "/api/next":
                cand = session.next_candidate(groups)
            else:
                self.send_error(404)
                return
            data = json.dumps(session.item(cand), ensure_ascii=False).encode("utf-8")
            self._send(data, "application/json; charset=utf-8")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=sorted(migrate_fit.CORPORA), default="export")
    parser.add_argument("--per-word", type=int, default=1, help="labels per word, group, parent")
    parser.add_argument("--seed", type=int, default=None, help="sentence order")
    parser.add_argument("--labels", default=str(hand_labels.HAND_LABELS), help="label file")
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to judge from a phone")
    parser.add_argument("--port", type=int, default=8790, help="not 8765, AnkiConnect's")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--anki-connect", default=anki_connect.URL, help="AnkiConnect's URL")
    args = parser.parse_args()

    corpus = migrate_fit.read_export(migrate_fit.CORPORA[args.corpus], Counter())
    stripped = (migrate_fit.html_stripping.strip_context_sentences(s) for s, _ in corpus)
    sentences = list(dict.fromkeys(s for s in stripped if s.strip()))
    random.Random(args.seed).shuffle(sentences)
    # The checked subset has no note ids: its sentences find theirs in the export
    nids: dict[str, list[int]] = {}
    raws: dict[int, str] = {}
    for raw, ids in hand_labels.read_export_nids(migrate_fit.CORPORA["export"]).items():
        found = nids.setdefault(migrate_fit.html_stripping.strip_context_sentences(raw), [])
        found.extend(nid for nid in ids if nid not in found)
        raws.update((nid, raw) for nid in ids)
    # Names come out as the migration op makes them: whole, from the collection's lexicon
    lexicon = migrate_fit.export_name_lexicon()
    session = Session(
        sentences, lexicon, hand_labels.Path(args.labels), args.per_word, nids=nids, raws=raws
    )
    print(f"{len(sentences)} sentences, {len(session.labels)} labels in {args.labels}")
    if not nids:
        print("The export has no note ids: Open in Anki and Refetch need a fresh export.")
    threading.Thread(target=session.generate_all, daemon=True).start()

    anki = anki_connect.AnkiConnect(args.anki_connect)
    paths = [migrate_fit.CORPORA["export"], migrate_fit.CORPORA["checked"]]
    # HTTPServer sets SO_REUSEADDR, which on Windows lets it bind a port another server (Anki
    # Connect) holds without error, and the browser then reaches that server instead.
    HTTPServer.allow_reuse_address = False
    handler = make_handler(session, anki, anki_connect.load_config(), paths)
    server = HTTPServer((args.host, args.port), handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Serving {url} (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
