"""Judging triage words by hand, in the browser, a round at a time.

    py -3.10 word_array/research/triage_judge.py pick [--uncertain 300]   # the next round
    py -3.10 word_array/research/triage_judge.py serve [--host 0.0.0.0]   # judge it
    py -3.10 word_array/research/triage_judge.py status
    py -3.10 word_array/research/triage_judge.py export-queue --dir DIR   # for the artifact page
    py -3.10 word_array/research/triage_judge.py import-labels --dir DIR  # its labels, saved
    py -3.10 word_array/research/triage_judge.py remap --old-words OLD.jsonl  # after a refresh

`pick` queues the next round in `judge_queue.jsonl`. The first round starts with a fixed random
100 of the unjudged notes: they are never trained on, so the model's accuracy on them is an
honest figure for the notes it will act on. Every round adds the `--uncertain` notes the latest
`predictions.jsonl` (triage_model.py) is least sure about: most near the learn/schedule line
(P(known) near `--learn-line`), the rest near the suspend line (P(suspend) near
`--suspend-line`), at most one sense or reading of a word per round.

`serve` shows one queued note at a time: the word and its reading, with its meaning, sentence
and other senses hidden until revealed. Suspend / Schedule / Learn go into
`hand_labels.jsonl`; Skip leaves the note queued; Undo takes back the last. Wrong data (label
`invalid`, with the user's note on what is wrong) is for a note whose own data is wrong, a
reading or sense the note should not have, a bad split: it leaves the triage (no model trains
on it, no decision is made for it) and triage_decide.py lists it for fixing. With `--host
0.0.0.0` the page is reachable from a phone on the same network, at the address printed; it
calls nothing else, so it works without Anki running.

The same judging works away from the PC through `triage_judge_page.html`, published as a private
claude.ai artifact whose database holds the queue and the labels. `export-queue` writes the queue's
notes as the documents of its `queue` collection (`queue-rR-PP.json`, CHUNK notes each, in queue
order, the kind left out so a note does not say whether it is a random one); a Claude session
writes them with its ArtifactData tool. The page writes one `labels/<nid>` document per judgement
and deletes it on Undo. `import-labels` reads those documents back, as the session's ArtifactData
`list` saved them (`--dir`, one JSON file each), and writes `hand_labels.jsonl` with each note's
kind and round from the queue: the same file `serve` writes.

A refresh of `words.jsonl` after the match op ran again can delete a note and make it anew, with
a new id, and the queue and the labels name notes by id. `remap` (given the words as they were)
moves each queued note that is gone onto its new note, in `judge_queue.jsonl` and
`hand_labels.jsonl`: the one new note of the same word and reading (the op rewords a meaning when
it makes the note anew), or, where the word has several, the one with the same meaning too; and writes the moves to `remap.json`, for the
session that keeps the artifact's database to move its label documents the same way. A gone note
with no such match, or more than one, is listed and left: its label names a note that is no more.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import triage_data as td
import triage_features as tf

QUEUE = "judge_queue.jsonl"
HAND_LABELS = "hand_labels.jsonl"
RANDOM_N = 100
RANDOM_SEED = 100
ACTIONS = ("suspend", "schedule", "learn")
INVALID = "invalid"
VERDICTS = ACTIONS + (INVALID,)
CHUNK = 50


def read_state():
    queue = td.read_jsonl(td.data_file(QUEUE))
    labels = td.read_jsonl(td.data_file(HAND_LABELS))
    return queue, labels


def judged_nids() -> set[int]:
    return {r["nid"] for r in td.read_jsonl(td.data_file("labels.jsonl"))}


def random_holdout(words: list[dict], judged: set[int]) -> list[int]:
    """The fixed random 100 of the notes the user had not judged in Anki."""
    pool = sorted(w["nid"] for w in words if w["nid"] not in judged)
    return random.Random(RANDOM_SEED).sample(pool, RANDOM_N)


def holdout_nids() -> set[int]:
    return {r["nid"] for r in td.read_jsonl(td.data_file(QUEUE)) if r["kind"] == "random"}


def pick(args) -> int:
    words = td.read_jsonl(td.data_file("words.jsonl"))
    by_nid = {w["nid"]: w for w in words}
    queue, labels = read_state()
    judged = judged_nids()
    done = {r["nid"] for r in labels} | {r["nid"] for r in queue}
    round_no = max((r["round"] for r in queue), default=-1) + 1
    new: list[dict] = []
    if not any(r["kind"] == "random" for r in queue):
        new += [{"nid": n, "kind": "random", "round": round_no}
                for n in random_holdout(words, judged)]
    preds = td.read_jsonl(td.data_file("predictions.jsonl"))
    if preds and args.uncertain:
        # A proper noun is wrong data by the user's rule, never worth a judgement
        taken = {r["nid"] for r in new} | done | tf.proper_noun_nids(words)
        pool = [p for p in preds if not p["labelled"] and p["nid"] not in taken]
        n_suspend = args.uncertain // 4
        seen_bases: set[str] = set()

        def take(rows, n, kind):
            got = 0
            for p in rows:
                base = by_nid[p["nid"]]["base"]
                if base in seen_bases or p["nid"] in taken:
                    continue
                seen_bases.add(base)
                taken.add(p["nid"])
                new.append({"nid": p["nid"], "kind": kind, "round": round_no})
                got += 1
                if got >= n:
                    break

        take(sorted(pool, key=lambda p: abs(p["p_known"] - args.learn_line)),
             args.uncertain - n_suspend, "uncertain-learn")
        take(sorted(pool, key=lambda p: abs(p["p_suspend"] - args.suspend_line)),
             n_suspend, "uncertain-suspend")
    # Shuffled, so that the random ones and the uncertain ones are judged alike
    random.Random(round_no).shuffle(new)
    with td.data_file(QUEUE).open("a", encoding="utf-8") as out:
        for row in new:
            out.write(json.dumps(row) + "\n")
    kinds: dict = {}
    for r in new:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"round {round_no}: queued {len(new)} ({kinds})")
    return 0


def frequencies() -> dict[int, dict]:
    return {r["nid"]: r for r in td.read_jsonl(td.data_file("frequency.jsonl"))}


def item(w: dict, kind: str, freq: Optional[dict] = None) -> dict:
    senses = [s for s in w["siblings"] if s["relation"] == td.MEANING]
    readings = [s for s in w["siblings"] if s["relation"] != td.MEANING]
    return {
        "nid": w["nid"],
        "key": w["key"],
        "markers": w["markers"],
        "word": w["word"] or w["kanjified"],
        "spelling": w["kanjified"] if w["kanjified"] != (w["word"] or w["kanjified"]) else "",
        "reading": w["reading"],
        "pos": w["pos"],
        "meaning": w["meaning"],
        "meaning_jp": w["meaning_jp"],
        "sentence": w["sentence"],
        "translation": w["sentence_translation"],
        "senses": [{"meaning": s["meaning"], "studied": s["reviewed"]} for s in senses][:10],
        "readings": [{"key": s["key"], "reading": s["reading"], "studied": s["reviewed"]}
                     for s in readings][:6],
        "kind": kind,
        # The ranks the model reads too, shown because the user asked to see them
        "freq": {name: (freq or {}).get(f"freq_{name}_rank")
                 for name in ("jiten", "tubelex", "bccwj")},
    }


class Session:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.words = {w["nid"]: w for w in td.read_jsonl(td.data_file("words.jsonl"))}
        self.freq = frequencies()
        self.queue, self.labels = read_state()
        self.skipped: set[int] = set()
        self.history: list[int] = []

    def pending(self) -> list[dict]:
        done = {r["nid"] for r in self.labels}
        return [r for r in self.queue if r["nid"] not in done and r["nid"] in self.words]

    def next(self) -> dict:
        with self.lock:
            pending = self.pending()
            fresh = [r for r in pending if r["nid"] not in self.skipped] or pending
            stats = {"left": len(pending), "labels": len(self.labels),
                     "now": len(self.history)}
            if not fresh:
                return {"item": None, "stats": stats}
            row = fresh[0]
            one = item(self.words[row["nid"]], row["kind"], self.freq.get(row["nid"]))
            return {"item": one, "stats": stats}

    def judge(self, nid: int, action: str, note: str = "") -> None:
        with self.lock:
            kind = next((r["kind"] for r in self.queue if r["nid"] == nid), "")
            rnd = next((r["round"] for r in self.queue if r["nid"] == nid), None)
            if action == "skip":
                self.skipped.add(nid)
                return
            self.labels = [r for r in self.labels if r["nid"] != nid]
            row = {"nid": nid, "label": action, "kind": kind, "round": rnd,
                   "t": int(time.time())}
            if action == INVALID:
                row["note"] = note
            self.labels.append(row)
            self.history.append(nid)
            self._save()

    def undo(self) -> None:
        with self.lock:
            if not self.history:
                return
            nid = self.history.pop()
            self.labels = [r for r in self.labels if r["nid"] != nid]
            self.skipped.discard(nid)
            # Back to the front of the queue
            self.queue = ([r for r in self.queue if r["nid"] == nid]
                          + [r for r in self.queue if r["nid"] != nid])
            self._save()

    def _save(self) -> None:
        td.write_jsonl(td.data_file(HAND_LABELS), self.labels)


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vocab triage</title>
<style>
:root { --bg:#f7f7f5; --fg:#1d1d1f; --muted:#6b6b70; --card:#fff; --line:#dcdcd8;
  --sus:#6b6b70; --sch:#2e6fd8; --lrn:#c0392b; }
@media (prefers-color-scheme: dark) { :root { --bg:#18181a; --fg:#ececec; --muted:#9a9aa0;
  --card:#232326; --line:#3a3a3e; } }
body { background:var(--bg); color:var(--fg); margin:0; padding:16px;
  font:16px/1.5 system-ui, "Segoe UI", "Yu Gothic UI", "Meiryo", sans-serif; }
main { max-width:680px; margin:0 auto; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:20px; margin:12px 0; min-height:180px; }
.word { font-size:40px; line-height:1.3; }
.reading { font-size:22px; color:var(--muted); }
.pos { font-size:13px; color:var(--muted); margin-top:4px; }
.hidden { display:none; }
.meaning { font-size:18px; margin-top:14px; }
.jp, .sentence, .tr, .senses { margin-top:10px; font-size:15px; }
.tr, .senses, .muted { color:var(--muted); }
.buttons { display:flex; gap:8px; margin-top:8px; }
button { font-size:18px; padding:16px 10px; border-radius:10px; border:1px solid var(--line);
  background:var(--card); color:var(--fg); cursor:pointer; flex:1; }
button.sus { background:var(--sus); color:#fff; border-color:var(--sus); }
button.sch { background:var(--sch); color:#fff; border-color:var(--sch); }
button.lrn { background:var(--lrn); color:#fff; border-color:var(--lrn); }
.small button { font-size:14px; padding:10px; }
.stats { font-size:13px; color:var(--muted); }
</style></head><body><main>
<div class="card" id="card">Loading...</div>
<div class="buttons"><button id="reveal" onclick="reveal()">Show meaning <small>(space)</small></button></div>
<div class="buttons">
  <button class="sus" onclick="send('suspend')">Suspend <small>(1)</small></button>
  <button class="sch" onclick="send('schedule')">Schedule <small>(2)</small></button>
  <button class="lrn" onclick="send('learn')">Learn <small>(3)</small></button>
</div>
<div class="buttons small">
  <button onclick="wrong()">Wrong data <small>(4)</small></button>
</div>
<div class="buttons small">
  <button onclick="send('skip')">Skip <small>(S)</small></button>
  <button onclick="undo()">Undo <small>(U)</small></button>
</div>
<p class="stats" id="stats"></p>
<p class="stats">Suspend: nothing to learn. Schedule: I think I know it; test me years out.
Learn: I don't know it.</p>
</main><script>
let current = null;
const esc = s => (s || "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"})[c]);
function show(data) {
  current = data.item;
  const s = data.stats;
  document.getElementById("stats").textContent =
    `${s.now} judged now, ${s.labels} saved, ${s.left} left in the queue`;
  const card = document.getElementById("card");
  if (!current) { card.textContent = "Nothing queued: run pick for the next round."; return; }
  const senses = current.senses.map(x => `<li>${esc(x.meaning)}${x.studied ? " <b>(studied)</b>" : ""}</li>`).join("");
  const readings = current.readings.map(x => `<li>${esc(x.key)} [${esc(x.reading)}]${x.studied ? " <b>(studied)</b>" : ""}</li>`).join("");
  card.innerHTML = `<div class="word">${esc(current.word)}</div>
    <div class="reading">${esc(current.reading)}${current.spelling ? " / " + esc(current.spelling) : ""}</div>
    <div class="pos">${esc(current.pos)}${rank(current.freq)}</div>
    <div id="back" class="hidden">
      <div class="meaning">${esc(current.meaning)}</div>
      <div class="jp">${esc(current.meaning_jp)}</div>
      <div class="sentence">${esc(current.sentence)}</div>
      <div class="tr">${esc(current.translation)}</div>
      ${senses ? `<div class="senses">Other senses:<ul>${senses}</ul></div>` : ""}
      ${readings ? `<div class="senses">Same spelling or kanji:<ul>${readings}</ul></div>` : ""}
    </div>`;
  document.getElementById("reveal").disabled = false;
}
function rank(f) {
  if (!f) return "";
  for (const [k, name] of [["jiten", "Jiten"], ["tubelex", "TUBELEX"], ["bccwj", "BCCWJ"]])
    if (f[k]) return ` · ${name} #${f[k]}`;
  return " · no frequency rank";
}
function reveal() {
  const back = document.getElementById("back");
  if (back) back.classList.remove("hidden");
  document.getElementById("reveal").disabled = true;
}
async function post(url, body) {
  const r = await fetch(url, {method: "POST", body: JSON.stringify(body || {})});
  show(await r.json());
}
function send(action) { if (current) post("/api/judge", {nid: current.nid, action}); }
function wrong() {
  if (!current) return;
  const note = prompt("What is wrong with this note? (optional)", "");
  if (note !== null) post("/api/judge", {nid: current.nid, action: "invalid", note});
}
function undo() { post("/api/undo"); }
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === " ") { e.preventDefault(); reveal(); }
  else if (k === "1") send("suspend");
  else if (k === "2") send("schedule");
  else if (k === "3") send("learn");
  else if (k === "4") wrong();
  else if (k === "s") send("skip");
  else if (k === "u") undo();
});
post("/api/next");
</script></body></html>"""


def make_handler(session: Session):
    page = PAGE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
            if path == "/api/judge" and body.get("action") in VERDICTS + ("skip",):
                session.judge(int(body.get("nid", 0)), body["action"], str(body.get("note") or ""))
            elif path == "/api/undo":
                session.undo()
            elif path != "/api/next":
                self.send_error(404)
                return
            data = json.dumps(session.next(), ensure_ascii=False).encode("utf-8")
            self._send(data, "application/json; charset=utf-8")

    return Handler


def lan_address() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # no packet is sent: this only picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def serve(args) -> int:
    session = Session()
    HTTPServer.allow_reuse_address = False
    server = HTTPServer((args.host, args.port), make_handler(session))
    print(f"{len(session.pending())} queued, {len(session.labels)} judged so far")
    print(f"Serving http://127.0.0.1:{args.port}/ (Ctrl+C to stop)")
    if args.host == "0.0.0.0":
        print(f"From a phone on the same network: http://{lan_address()}:{args.port}/")
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def status(_args) -> int:
    queue, labels = read_state()
    done = {r["nid"]: r for r in labels}
    by_kind: dict = {}
    for r in queue:
        cell = by_kind.setdefault((r["round"], r["kind"]), [0, 0])
        cell[0] += 1
        cell[1] += r["nid"] in done
    for (rnd, kind), (n, d) in sorted(by_kind.items()):
        print(f"round {rnd} {kind:18} {d}/{n} judged")
    counts: dict = {}
    for r in labels:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print("labels:", counts)
    return 0


def export_queue(args) -> int:
    """The queue's notes as the artifact's `queue` documents, one file each."""
    words = {w["nid"]: w for w in td.read_jsonl(td.data_file("words.jsonl"))}
    freq = frequencies()
    queue, _ = read_state()
    args.dir.mkdir(parents=True, exist_ok=True)
    rounds: dict[int, list[dict]] = {}
    for order, row in enumerate(queue):
        if row["nid"] not in words:
            continue
        one = item(words[row["nid"]], row["kind"], freq.get(row["nid"]))
        del one["kind"]
        one["order"] = order
        rounds.setdefault(row["round"], []).append(one)
    written = 0
    for rnd, items in sorted(rounds.items()):
        for part in range(0, len(items), CHUNK):
            doc = {"round": rnd, "part": part // CHUNK, "items": items[part : part + CHUNK]}
            name = f"queue-r{rnd}-{part // CHUNK:02d}"
            (args.dir / f"{name}.json").write_text(json.dumps(doc, ensure_ascii=False),
                                                   encoding="utf-8")
            written += 1
    print(f"wrote {written} queue documents for {sum(map(len, rounds.values()))} notes to"
          f" {args.dir}")
    return 0


def import_labels(args) -> int:
    """The page's `labels` documents, as saved under `--dir`, into `hand_labels.jsonl`."""
    queue, _ = read_state()
    placed = {r["nid"]: r for r in queue}
    labels, odd = [], 0
    for path in sorted(args.dir.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc = doc.get("data", doc) if isinstance(doc.get("data"), dict) else doc
        nid = int(doc.get("nid", 0))
        if nid not in placed or doc.get("label") not in VERDICTS:
            odd += 1
            continue
        row = {"nid": nid, "label": doc["label"], "kind": placed[nid]["kind"],
               "round": placed[nid]["round"], "t": int(doc.get("t", 0)) // 1000}
        if doc["label"] == INVALID:
            row["note"] = str(doc.get("note") or "")
        if doc.get("by"):
            row["by"] = str(doc["by"])  # "rule": set by a session on the user's rule, not judged
        labels.append(row)
    labels.sort(key=lambda r: r["t"])
    td.write_jsonl(td.data_file(HAND_LABELS), labels)
    print(f"{len(labels)} labels written to {HAND_LABELS}"
          + (f"; {odd} documents skipped (not queued, or no label)" if odd else ""))
    return status(args)


def word_reading(w: dict) -> tuple:
    return (w["word"] or w["kanjified"], w["reading"])


def remap(args) -> int:
    words = {w["nid"]: w for w in td.read_jsonl(td.data_file("words.jsonl"))}
    old = {w["nid"]: w for w in td.read_jsonl(args.old_words)}
    by_word: dict[tuple, list[int]] = {}
    for w in words.values():
        by_word.setdefault(word_reading(w), []).append(w["nid"])
    queue, labels = read_state()
    queued = {r["nid"] for r in queue}
    moved: dict[int, int] = {}
    lost: list[int] = []
    for r in queue:
        nid = r["nid"]
        if nid in words:
            continue
        found = [n for n in by_word.get(word_reading(old[nid]), []) if n not in queued] \
            if nid in old else []
        if len(found) > 1:
            found = [n for n in found if words[n]["meaning"] == old[nid]["meaning"]]
        if len(found) == 1 and found[0] not in moved.values():
            moved[nid] = found[0]
        else:
            lost.append(nid)
    for row in queue + labels:
        row["nid"] = moved.get(row["nid"], row["nid"])
    td.write_jsonl(td.data_file(QUEUE), queue)
    td.write_jsonl(td.data_file(HAND_LABELS), labels)
    td.data_file("remap.json").write_text(
        json.dumps({str(a): b for a, b in moved.items()}, indent=1) + "\n", encoding="utf-8")
    judged = {r["nid"] for r in labels}
    print(f"{len(moved)} queued notes moved onto their new note ({sum(n in judged for n in moved.values())}"
          f" judged), {len(lost)} gone with no single match:")
    for nid in lost:
        w = old.get(nid, {})
        print(f"  nid {nid} {w.get('key', '?')} ({w.get('reading', '?')})")
    return status(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--uncertain", type=int, default=300)
    p.add_argument("--learn-line", type=float, default=0.5)
    p.add_argument("--suspend-line", type=float, default=0.5)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to judge from a phone")
    s.add_argument("--port", type=int, default=8791, help="not 8765, AnkiConnect's")
    s.add_argument("--no-browser", action="store_true")
    sub.add_parser("status")
    e = sub.add_parser("export-queue")
    e.add_argument("--dir", type=Path, required=True)
    i = sub.add_parser("import-labels")
    i.add_argument("--dir", type=Path, required=True)
    r = sub.add_parser("remap")
    r.add_argument("--old-words", type=Path, required=True,
                   help="words.jsonl as it was before the refresh")
    args = parser.parse_args()
    return {"pick": pick, "serve": serve, "status": status, "export-queue": export_queue,
            "import-labels": import_labels, "remap": remap}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
