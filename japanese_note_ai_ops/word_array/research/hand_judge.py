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
"""

import argparse
import html
import json
import random
import re
import sys
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import NamedTuple, Optional
from urllib.parse import parse_qs, urlparse

import hand_labels
import migrate_fit
from _bootstrap import load

judge_v2 = load("judge_v2")
match_flags = load("match_flags")

DEFAULT_GROUPS = ("noun-sub", "noun-phrase", "prefix-verb", "suffix-verb", "affix")
GROUPS = [g for g in judge_v2.POS_RULES]
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


def ruby(raw_text: str) -> str:
    text = html.unescape(TAG_RE.sub("", raw_text))
    out, pos = [], 0
    for m in RUBY_RE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        out.append(f"<ruby>{html.escape(m[1])}<rt>{html.escape(m[2])}</rt></ruby>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out).replace(" ", "")


def render(items: list, target: list, parents: list[list]) -> str:
    parts = []
    for elem in items:
        if elem is target:
            parts.append(f"<mark>{ruby(elem[0])}</mark>")
        elif len(elem) > 1 and any(elem is p for p in parents):
            parts.append(f'<span class="parent">{render(elem[5], target, parents)}</span>')
        else:
            parts.append(ruby(elem[0]))
    return "".join(parts)


def describe(elem: list) -> str:
    return f"{elem[2]} [{elem[3]}], {elem[1]}"


class Session:
    def __init__(self, sentences: list[str], labels_path, per_word: int):
        self.sentences = sentences
        self.next_sentence = 0
        self.labels_path = labels_path
        self.per_word = per_word
        self.labels = hand_labels.read_labels(labels_path)
        self.pool: list[Candidate] = []
        self.done: set[tuple] = {hand_labels.label_key(row) for row in self.labels}
        self.word_counts: Counter[tuple] = Counter(
            word_key(row["group"], tuple(row["path"]), row["reading"]) for row in self.labels
        )
        self.history: list[tuple[Candidate, str]] = []
        self.offered: dict[int, Candidate] = {}
        self.judged_now = 0

    def _generate_more(self) -> bool:
        if self.next_sentence >= len(self.sentences):
            return False
        sentence = self.sentences[self.next_sentence]
        self.next_sentence += 1
        try:
            arr = migrate_fit.generator.generate(sentence)
        except Exception as exc:  # a crash here is migrate_fit's business, not the GUI's
            print(f"generate failed: {exc!r}: {sentence}", file=sys.stderr)
            return True
        for placed in hand_labels.placed_words(arr):
            elem = placed.elem
            if elem[1] in judge_v2.AUTO_DONT_MATCH_POS:
                continue
            if match_flags.match_state(elem) != match_flags.MatchState.UNJUDGED:
                continue
            group = judge_v2.rule_group(elem, placed.parents)
            self.pool.append(Candidate(sentence, arr, placed, group))
        return True

    def _offerable(self, cand: Candidate, groups: set[str]) -> bool:
        p = cand.placed
        return (
            cand.group in groups
            and hand_labels.placed_key(cand.sentence, p) not in self.done
            and self.word_counts[word_key(cand.group, p.path, p.elem[3])] < self.per_word
        )

    def next_candidate(self, groups: set[str]) -> Optional[Candidate]:
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
        stats = {
            "labels": len(self.labels),
            "now": self.judged_now,
            "by_group": dict(Counter(row["group"] for row in self.labels).most_common()),
            "sentences": f"{self.next_sentence}/{len(self.sentences)}",
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
                "rules": judge_v2.POS_RULES[cand.group],
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
</style></head><body><main>
<div class="groups" id="groups"></div>
<div class="card" id="card">Loading...</div>
<div class="buttons">
  <button class="match" onclick="send('match')">Match <small>(M)</small></button>
  <button class="dont" onclick="send('dontmatch')">Don't match <small>(D)</small></button>
  <button onclick="send('skip')">Skip <small>(S)</small></button>
  <button onclick="undo()">Undo <small>(U)</small></button>
</div>
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
    Object.entries(s.by_group).map(([g, n]) => `${g} ${n}`).join(", ");
  const card = document.getElementById("card");
  if (!current) { card.textContent = "Nothing left to offer in the ticked groups."; return; }
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
function send(label) { if (current) post("/api/judge", {id: current.id, label}); }
function undo() { post("/api/undo", {}); }
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey || e.target.tagName === "INPUT") return;
  const k = e.key.toLowerCase();
  if (k === "m") send("match");
  else if (k === "d") send("dontmatch");
  else if (k === "s") send("skip");
  else if (k === "u") undo();
});
post("/api/next", {});
</script></body></html>"""


def make_handler(session: Session):
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
    args = parser.parse_args()

    corpus = migrate_fit.read_export(migrate_fit.CORPORA[args.corpus], Counter())
    stripped = (migrate_fit.html_stripping.strip_context_sentences(s) for s, _ in corpus)
    sentences = list(dict.fromkeys(s for s in stripped if s.strip()))
    random.Random(args.seed).shuffle(sentences)
    session = Session(sentences, hand_labels.Path(args.labels), args.per_word)
    print(f"{len(sentences)} sentences, {len(session.labels)} labels in {args.labels}")

    # HTTPServer sets SO_REUSEADDR, which on Windows lets it bind a port another server (Anki
    # Connect) holds without error, and the browser then reaches that server instead.
    HTTPServer.allow_reuse_address = False
    server = HTTPServer((args.host, args.port), make_handler(session))
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
