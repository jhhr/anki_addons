"""Labelling by ear which reading the furigana audio eval's cards say, in the browser.

    python word_array/research/furigana_audio_label.py queue [--models A B] [--quorum 1]
    python word_array/research/furigana_audio_label.py serve [--host 0.0.0.0] [--port 8791]

`queue`, run where the scored runs are (`furigana_audio_score.py MODEL` writes their words),
picks the words worth a person's ear and writes `label_queue.jsonl` beside the selection, a card
a row. Under the op's rule over the given models' runs (`furigana_audio_score.outcome`) that is
every word the rule writes otherwise than the draft, every word it sends to review, every draft
it writes on fewer than every model's hearing, and a sample of the drafts every model heard,
which says how often agreement is wrong. A word whose reading the captions give is left out,
the caption being its label, and so is a card whose transcripts do not match its line. Each word
carries the readings to choose from: the draft, a reading the dictionary has that a model
heard, and what each model heard where it sounds like none of those.

`serve` needs only the queue, the clips and the standard library, so it runs on the machine
with the test data checkout and no model environment. It serves a page on localhost that plays
a card's clip and asks, word by word, which reading was said: one of the readings offered,
typed in, "can't tell" or "not said". The readings are in kana order and do not say where they
came from, so the draft gets no head start; a toggle shows that. Each label goes to
`labels.jsonl` as it is given, and `furigana_audio_score.py` scores the runs and the rule
against the labels where there are any. Cards come in the queue's order, the corrections first,
then the reviews, the drafts not every model heard, the sample, so stopping anywhere leaves the
most useful labels done. Undo takes back the last label; a skipped card comes back next time.

The clips are Ogg Opus, which Chrome, Edge and Firefox play.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import unquote, urlparse

import furigana_audio as fa
from furigana_audio_run import RUNS

# The models whose runs the rule combines, as the results recommend, and its quorum
MODELS = ("kana-anime-whisper", "ruby-mora")
QUORUM = 1
# Of the drafts every model heard, the share to label: enough to see how often agreement errs
SAMPLE = 0.1
ORDER = {bucket: n for n, bucket in enumerate(("correction", "review", "partial", "agreed"))}
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def choices(rows: Sequence[dict], models: Sequence[str]) -> list[dict]:
    """The readings to offer for one word, given each model's row of it: the draft, every
    other reading the op may write that a model heard, and each model's own hearing where it
    sounds like none of those. Readings that sound alike are one choice, spelt as the
    dictionary has it; each says where it came from, for the page's toggle."""
    from furigana_audio_score import sound

    found: dict[tuple, dict] = {}

    def add(kana: str, source: str) -> None:
        kana = fa.to_hiragana(kana)
        key = sound(kana)
        if not key:
            return
        entry = found.setdefault(key, {"kana": kana, "from": []})
        if source not in entry["from"]:
            entry["from"].append(source)

    draft = rows[0]["sudachi"]
    if not fa.KANJI_RE.search(draft):
        add(draft, "draft")
    for model, row in zip(models, rows):
        for reading, heard in row["op_hears"].items():
            if heard and reading != "draft":
                add(reading, "dictionary")
                add(reading, model)
            elif heard:
                add(draft, model)
    for model, row in zip(models, rows):
        if row["heard"]:
            add(row["heard"], model)
    return sorted(found.values(), key=lambda c: c["kana"])


def build_queue(
    models: Sequence[str] = MODELS,
    quorum: int = QUORUM,
    sample: float = SAMPLE,
    seed: int = 0,
) -> list[dict]:
    """The cards with a word to label, in the order to label them."""
    from furigana_audio_score import outcome

    runs = []
    for m in models:
        rows = fa.read_jsonl(RUNS / f"{m}.words.jsonl")
        if not rows or "op_hears" not in rows[0]:
            raise SystemExit(f"{m}: score it first (furigana_audio_score.py {m})")
        runs.append({fa.word_key(r): r for r in rows})
    rng = random.Random(seed)
    cards: list[tuple[int, float, dict]] = []
    for card in fa.read_jsonl(fa.SELECTION):
        words = []
        for w in card["words"]:
            key = (card["id"], w["line"], w["start"])
            found = [run.get(key) for run in runs]
            heard = [r for r in found if r is not None]
            if len(heard) < len(runs) or not all(r["clean"] for r in heard) or heard[0]["gold"]:
                continue
            _, bucket = outcome(heard, quorum)
            if bucket == "agreed" and rng.random() >= sample:
                continue
            words.append(
                {
                    "line": w["line"],
                    "start": w["start"],
                    "end": w["end"],
                    "surface": w["surface"],
                    "bucket": bucket,
                    "choices": choices(heard, models),
                }
            )
        if not words:
            continue
        lines = []
        for line in card["lines"]:
            text, readings = fa.split_readings(line)
            lines.append({"text": text, "ruby": [[r.start, r.end, r.reading] for r in readings]})
        bucket = min((w["bucket"] for w in words), key=ORDER.__getitem__)
        row = {
            "id": card["id"],
            "audio": card["audio"],
            "en": card["en"],
            "lines": lines,
            "bucket": bucket,
            "words": words,
        }
        # Random within a bucket, so that the labels given before stopping are a fair sample
        cards.append((ORDER[bucket], rng.random(), row))
    cards.sort(key=lambda c: c[:2])
    return [row for _, _, row in cards]


def queue_summary(cards: Sequence[dict]) -> str:
    counts = {b: 0 for b in ORDER}
    for card in cards:
        for w in card["words"]:
            counts[w["bucket"]] += 1
    words = sum(counts.values())
    parts = ", ".join(f"{b} {n}" for b, n in counts.items())
    return f"{len(cards)} cards, {words} words to label: {parts}"


class Session:
    """The queue, the labels given and this session's undo history and skipped cards."""

    def __init__(self, queue: list[dict], path: Path):
        self.queue = queue
        self.path = path
        self.labels = fa.read_labels(path)
        self.history: list[fa.WordKey] = []
        self.skipped: set[str] = set()
        self.cards = {card["id"]: card for card in queue}
        self.position = {
            (card["id"], w["line"], w["start"]): (n, i)
            for n, card in enumerate(queue)
            for i, w in enumerate(card["words"])
        }
        self.lock = threading.Lock()

    def card(self, card_id: str) -> dict:
        """A card for the page, each word with its label if it has one."""
        card = dict(self.cards[card_id])
        card["words"] = [
            {**w, "label": self.labels.get((card_id, w["line"], w["start"]))}
            for w in card["words"]
        ]
        return card

    def _open(self, card: dict) -> bool:
        return any((card["id"], w["line"], w["start"]) not in self.labels for w in card["words"])

    def next_card(self) -> Optional[dict]:
        for card in self.queue:
            if card["id"] not in self.skipped and self._open(card):
                return self.card(card["id"])
        return None

    def label(self, key: fa.WordKey, reading: Optional[str], verdict: str) -> None:
        card = self.cards[key[0]]
        word = next(w for w in card["words"] if (w["line"], w["start"]) == key[1:])
        self.labels[key] = {
            "id": key[0],
            "line": key[1],
            "start": key[2],
            "end": word["end"],
            "surface": word["surface"],
            "reading": fa.to_hiragana(reading) if verdict == "heard" and reading else None,
            "verdict": verdict,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.history.append(key)
        self._save()

    def undo(self) -> Optional[fa.WordKey]:
        while self.history:
            key = self.history.pop()
            if self.labels.pop(key, None) is not None:
                self.skipped.discard(key[0])
                self._save()
                return key
        return None

    def _save(self) -> None:
        """Every label, in the queue's order and stale ones last, written whole and then moved
        over the file, so a crash mid-write never loses the labels already given."""
        last = (len(self.queue), 0)
        rows = sorted(self.labels.values(), key=lambda r: self.position.get(fa.word_key(r), last))
        tmp = self.path.with_suffix(".jsonl.tmp")
        fa.write_jsonl(tmp, rows)
        os.replace(tmp, self.path)

    def stats(self) -> dict:
        done = {b: 0 for b in ORDER}
        total = {b: 0 for b in ORDER}
        for card in self.queue:
            for w in card["words"]:
                total[w["bucket"]] += 1
                done[w["bucket"]] += (card["id"], w["line"], w["start"]) in self.labels
        return {
            "buckets": [[b, done[b], total[b]] for b in ORDER],
            "labelled": sum(done.values()),
            "words": sum(total.values()),
            "now": len(self.history),
            "can_undo": bool(self.history),
        }

    def handle(self, path: str, body: dict) -> dict:
        """One of the page's requests: the card to show next, and the progress."""
        with self.lock:
            active = None
            if path == "/api/label":
                key = (str(body["id"]), int(body["line"]), int(body["start"]))
                verdict = body.get("verdict")
                if key in self.position and verdict in ("heard", "unsure", "not_said"):
                    reading = str(body.get("reading") or "").strip()
                    if verdict != "heard" or reading:
                        self.label(key, reading, verdict)
                if key[0] in self.cards and self._open(self.cards[key[0]]):
                    return {"card": self.card(key[0]), "stats": self.stats()}
            elif path == "/api/skip":
                self.skipped.add(str(body.get("id")))
            elif path == "/api/undo":
                undone = self.undo()
                if undone is not None:
                    active = [undone[1], undone[2]]
                    return {"card": self.card(undone[0]), "active": active, "stats": self.stats()}
            return {"card": self.next_card(), "stats": self.stats()}


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Furigana by ear</title>
<style>
:root { --bg:#f6f6f3; --fg:#1d1d1f; --muted:#67676d; --card:#fff; --line:#d9d9d4;
  --mark:#fde9a6; --active:#d9902b; --done:#2e7d55; --accent:#2c5c9a; }
@media (prefers-color-scheme: dark) { :root { --bg:#17171a; --fg:#ececec; --muted:#9d9da4;
  --card:#222226; --line:#393940; --mark:#5c4a12; --active:#f0b25a; --done:#6cc596;
  --accent:#8db5ec; } }
* { box-sizing:border-box; }
body { background:var(--bg); color:var(--fg); margin:0; padding:16px;
  font:15px/1.5 system-ui, "Segoe UI", "Yu Gothic UI", "Meiryo", sans-serif; }
main { max-width:760px; margin:0 auto; }
.stats { font-size:13px; color:var(--muted); display:flex; flex-wrap:wrap; gap:2px 14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:18px 20px; margin:12px 0; }
.player { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
.player audio { flex:1 1 260px; min-width:0; }
.lines { font-size:24px; line-height:2.3; margin:12px 0 4px; overflow-wrap:anywhere; }
.lines rt { font-size:11px; color:var(--muted); }
mark { background:var(--mark); color:inherit; border-radius:3px; padding:0 2px; cursor:pointer;
  white-space:nowrap; }
mark.active { outline:3px solid var(--active); outline-offset:1px; }
mark.done { background:transparent; border-bottom:3px solid var(--done); }
mark sup { font-size:11px; color:var(--muted); margin-left:1px; }
.word { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin:6px 0 10px; }
.word .surface { font-size:30px; }
.word .meta { color:var(--muted); font-size:13px; }
.choices, .buttons { display:flex; flex-wrap:wrap; gap:8px; }
button { font:inherit; font-size:17px; padding:10px 16px; border-radius:8px;
  border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
button:focus-visible, input:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.choices button { font-size:22px; padding:10px 18px; }
.choices button.picked { border-color:var(--done); box-shadow:inset 0 0 0 2px var(--done); }
.choices small, .buttons small { color:var(--muted); font-size:12px; }
.from { display:block; font-size:11px; color:var(--muted); }
.other { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
.other input { font-size:20px; padding:8px 10px; border-radius:8px; border:1px solid var(--line);
  background:var(--card); color:var(--fg); flex:1 1 180px; min-width:0; }
.buttons { margin-top:12px; }
.buttons button { font-size:15px; padding:8px 12px; }
details { margin-top:12px; color:var(--muted); font-size:14px; }
label.toggle { font-size:13px; color:var(--muted); display:inline-flex; gap:6px;
  align-items:center; }
.message { color:var(--muted); font-size:14px; min-height:1.5em; }
.empty { font-size:18px; }
</style></head><body><main>
<div class="stats" id="stats"></div>
<div class="card" id="card">Loading...</div>
<p class="message" id="message"></p>
<label class="toggle"><input type="checkbox" id="sources"> Show where each reading came from</label>
</main><script>
const $ = id => document.getElementById(id);
const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"};
const esc = s => String(s).replace(/[&<>"]/g, c => ESC[c]);
let card = null, active = 0, slow = false;
try { $("sources").checked = localStorage.getItem("sources") === "1" } catch (e) {}
$("sources").onchange = () => {
  try { localStorage.setItem("sources", $("sources").checked ? "1" : "0") } catch (e) {}
  if (card) renderWord();
};
function lineHtml(line, n) {
  const marks = card.words.map((w, i) => [w, i]).filter(([w]) => w.line === n);
  let out = "", pos = 0;
  const cuts = [...marks.map(([w, i]) => ({start: w.start, end: w.end, i})),
                ...line.ruby.map(([start, end, reading]) => ({start, end, reading}))]
    .sort((a, b) => a.start - b.start);
  for (const c of cuts) {
    if (c.start < pos) continue;
    out += esc(line.text.slice(pos, c.start));
    const text = esc(line.text.slice(c.start, c.end));
    if (c.reading !== undefined) out += `<ruby>${text}<rt>${esc(c.reading)}</rt></ruby>`;
    else {
      const w = card.words[c.i];
      const cls = [c.i === active ? "active" : "", w.label ? "done" : ""].join(" ");
      out += `<mark class="${cls}" data-i="${c.i}">${text}<sup>${c.i + 1}</sup></mark>`;
    }
    pos = c.end;
  }
  return out + esc(line.text.slice(pos));
}
function show(data) {
  card = data.card;
  if (!card) {
    $("card").innerHTML = '<p class="empty">Every word in the queue is labelled.</p>';
    showStats(data.stats);
    return;
  }
  active = card.words.findIndex(w => !w.label);
  if (data.active) {
    const [line, start] = data.active;
    active = card.words.findIndex(w => w.line === line && w.start === start);
  }
  if (active < 0) active = 0;
  $("card").innerHTML = `<div class="player"><audio id="audio" controls preload="auto"
      src="/audio/${encodeURIComponent(card.audio)}"></audio>
    <button onclick="replay()">Replay <small>(R)</small></button>
    <button id="slow" onclick="toggleSlow()">${slow ? "1×" : "0.75×"} <small>(S)</small></button>
    </div>
    <div class="lines" id="lines"></div><div id="word"></div>
    <div class="buttons">
      <button onclick="send('unsure')">Can't tell <small>(X)</small></button>
      <button onclick="send('not_said')">Not said <small>(N)</small></button>
      <button id="undo" onclick="undo()">Undo <small>(U)</small></button>
      <button onclick="skip()">Skip card <small>(K)</small></button>
    </div>
    <details><summary>English</summary>${esc(card.en).replace(/&lt;br&gt;/g, "<br>")}</details>`;
  const audio = $("audio");
  audio.playbackRate = slow ? 0.75 : 1;
  audio.play().catch(() => {});
  showStats(data.stats);
  renderWord();
}
function renderWord() {
  $("lines").innerHTML = card.lines.map(lineHtml).join("<br>");
  const w = card.words[active], sources = $("sources").checked;
  const picked = w.label && w.label.verdict === "heard" ? w.label.reading : null;
  const given = w.label ? " · labelled " + esc(w.label.reading || w.label.verdict) : "";
  $("word").innerHTML = `<div class="word"><span class="surface">${esc(w.surface)}</span>
      <span class="meta">word ${active + 1} of ${card.words.length}${given}</span></div>
    <div class="choices">${w.choices.map((c, i) =>
      `<button class="${c.kana === picked ? "picked" : ""}"
      onclick="pick(${i})">${i < 9 ? `<small>${i + 1}</small> ` : ""}${esc(c.kana)}${sources
      ? `<span class="from">${esc(c.from.join(" · "))}</span>` : ""}</button>`).join("")}</div>
    <div class="other"><input id="typed" type="text" lang="ja"
      placeholder="Other reading, in kana (T)"
      autocomplete="off"><button onclick="typed()">Save</button></div>`;
  $("typed").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.isComposing) typed();
    if (e.key === "Escape") e.target.blur();
  });
}
async function post(url, body) {
  const r = await fetch(url, {method: "POST", body: JSON.stringify(body)});
  return r.json();
}
async function label(verdict, reading) {
  if (!card) return;
  const w = card.words[active];
  $("message").textContent = "";
  const word = {id: card.id, line: w.line, start: w.start};
  const data = await post("/api/label", {...word, verdict, reading});
  if (data.card && card && data.card.id === card.id) {
    card = data.card;
    const next = card.words.findIndex((x, i) => i > active && !x.label);
    active = next >= 0 ? next : card.words.findIndex(x => !x.label);
    showStats(data.stats);
    renderWord();
  } else show(data);
}
function showStats(s) {
  $("stats").innerHTML =
    `<span>${s.labelled} of ${s.words} labelled, ${s.now} this session</span>` +
    s.buckets.map(([b, n, t]) => `<span>${esc(b)} ${n}/${t}</span>`).join("");
  if ($("undo")) $("undo").disabled = !s.can_undo;
}
const pick = i => card && label("heard", card.words[active].choices[i].kana);
const send = verdict => label(verdict, null);
function typed() {
  const value = $("typed").value.trim();
  if (!value) { $("message").textContent = "Type the reading in kana, or pick one."; return; }
  if (!/^[ぁ-ゖァ-ヺー]+$/.test(value)) { $("message").textContent = "Kana only, please."; return; }
  label("heard", value);
}
async function undo() { show(await post("/api/undo", {})); }
async function skip() { if (card) show(await post("/api/skip", {id: card.id})); }
function replay() { const a = $("audio"); if (a) { a.currentTime = 0; a.play().catch(() => {}); } }
function toggleSlow() {
  slow = !slow;
  const a = $("audio");
  if (a) a.playbackRate = slow ? 0.75 : 1;
  $("slow").innerHTML = `${slow ? "1×" : "0.75×"} <small>(S)</small>`;
}
$("card").addEventListener("click", e => {
  const m = e.target.closest("mark");
  if (m && card) { active = Number(m.dataset.i); renderWord(); }
});
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey || e.target.type === "text" || !card) return;
  const k = e.key.toLowerCase();
  // a focused button takes its own Space and Enter
  if (e.target.tagName === "BUTTON" && (k === " " || k === "enter")) return;
  if (/^[1-9]$/.test(k) && Number(k) <= card.words[active].choices.length) pick(Number(k) - 1);
  else if (k === "x") send("unsure");
  else if (k === "n") send("not_said");
  else if (k === "u") undo();
  else if (k === "k") skip();
  else if (k === "r") replay();
  else if (k === "s") toggleSlow();
  else if (k === "t") { e.preventDefault(); $("typed").focus(); }
  else if (k === " ") {
    e.preventDefault();
    const a = $("audio");
    if (a) a.paused ? a.play().catch(() => {}) : a.pause();
  }
  else if (k === "arrowright" || k === "arrowleft") {
    active = (active + (k === "arrowright" ? 1 : card.words.length - 1)) % card.words.length;
    renderWord();
  }
});
post("/api/next", {}).then(show);
</script></body></html>"""


def make_handler(session: Session, audio_dir: Path):
    page = PAGE.encode("utf-8")
    clips = {card["audio"] for card in session.queue}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, content_type: str, status: int = 200, extra=()) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._send(page, "text/html; charset=utf-8")
                return
            name = unquote(path[len("/audio/") :]) if path.startswith("/audio/") else ""
            if name not in clips or not (audio_dir / name).is_file():
                self.send_error(404)
                return
            self._clip((audio_dir / name).read_bytes())

        def _clip(self, data: bytes) -> None:
            """The clip, or the byte range asked for: a player that seeks asks for ranges."""
            m = RANGE_RE.match(self.headers.get("Range") or "")
            extra = [("Accept-Ranges", "bytes"), ("Cache-Control", "no-cache")]
            if not m or not (m.group(1) or m.group(2)):
                self._send(data, "audio/ogg", extra=extra)
                return
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), len(data) - 1) if m.group(2) else len(data) - 1
            else:
                start, end = max(0, len(data) - int(m.group(2))), len(data) - 1
            if start >= len(data) or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            extra.append(("Content-Range", f"bytes {start}-{end}/{len(data)}"))
            self._send(data[start : end + 1], "audio/ogg", 206, extra)

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/api/next", "/api/label", "/api/skip", "/api/undo"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                data = session.handle(path, body)
            except (ValueError, KeyError, TypeError) as e:
                self.send_error(400, str(e))
                return
            self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")

    return Handler


def serve(args: argparse.Namespace) -> int:
    queue = fa.read_jsonl(fa.LABEL_QUEUE)
    if not queue:
        print(f"{fa.LABEL_QUEUE} is missing or empty: run this script's queue", file=sys.stderr)
        return 2
    missing = sorted({c["audio"] for c in queue if not (fa.AUDIO / c["audio"]).is_file()})
    if missing:
        print(f"{len(missing)} clips are not in {fa.AUDIO}: {missing[:3]}", file=sys.stderr)
        return 2
    session = Session(queue, Path(args.labels))
    stats = session.stats()
    print(f"{stats['labelled']} of {stats['words']} words labelled in {args.labels}")
    # HTTPServer sets SO_REUSEADDR, which on Windows lets it bind a port another server holds
    # without error, and the browser then reaches that server instead (see hand_judge.py)
    ThreadingHTTPServer.allow_reuse_address = False
    server = ThreadingHTTPServer((args.host, args.port), make_handler(session, fa.AUDIO))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Serving {url} (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    q = sub.add_parser("queue", help="pick the words to label, from the scored runs")
    q.add_argument("--models", nargs="+", default=list(MODELS), help="the runs the rule combines")
    q.add_argument("--quorum", type=int, default=QUORUM, help="models that must hear a reading")
    q.add_argument("--sample", type=float, default=SAMPLE, help="share of agreed drafts to label")
    q.add_argument("--seed", type=int, default=0)
    s = sub.add_parser("serve", help="the labelling page")
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to label from a phone")
    s.add_argument("--port", type=int, default=8791, help="not 8765, AnkiConnect's")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--labels", default=str(fa.LABELS), help="the label file (the eval's)")
    args = ap.parse_args(argv)
    if args.command == "serve":
        return serve(args)
    cards = build_queue(args.models, args.quorum, args.sample, args.seed)
    fa.write_jsonl(fa.LABEL_QUEUE, cards)
    print(f"{fa.LABEL_QUEUE}: {queue_summary(cards)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
