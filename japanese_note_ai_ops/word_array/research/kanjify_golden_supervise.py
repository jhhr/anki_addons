"""Supervises the kanjify golden set's cloud sessions from the user's machine, unattended.

Cloud sessions stop when the account's usage limit is reached, and nothing restarts a turn the
limit cut off; a plain process here outlives it, and `agent_queue.py --wait-for-reset` sleeps
until the reset the CLI names. So what must happen overnight happens here, in a loop, every
few minutes, with no Claude session needed:

1. Sync: the cloud sessions' branches of the test data (`origin/claude/*`) are merged into
   `main` with this machine's own new results, and `main` is pushed. The sessions merge `main`
   before taking items (`cloud_runbook.md`), so an item done anywhere is skipped everywhere.
2. Step 2's settings, once the pilot's batch agents have answered (`pilot_batch`): batches of
   30 unless their agreement with the hand-fixed labels (span F1) is more than 2 points under
   batches of 10's, then 10, or 20 when 10 would not finish by --deadline on --parallel
   subagents; effort high unless batch agents are more than 5 points under the single-sentence
   xhigh agents, then xhigh when it still fits. Written to `collated/step2_settings.json`.
3. Once every word is decided (or at least 95% and none new for 30 minutes) and the settings
   are chosen: the inventory at that batch size, collate, `batches` rendered at that effort,
   committed and pushed to `main`, where the sessions under /loop pick it up.
4. Fallback: a shard whose cloud branch has pushed nothing for --idle minutes while its queue
   has items left (its session stopped at the limit, or died) is worked here by
   `agent_queue.py --shard I/N --wait-for-reset` until the branch moves again.
5. When every step 2 item has a result: a last collate, committed with the rest, and exit.

    py -3.10 word_array/research/kanjify_golden_supervise.py [--shards 4] [--deadline 18:00]
        [--parallel 32] [--idle 60] [--interval 300]

Logs to `evals/kanjify_golden/supervisor.log`. Stop it with Ctrl+C; a fallback agent_queue it
started stops at the STOP file it then writes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from _bootstrap import ADDON_ROOT

import kanjify_golden as golden

RESEARCH = Path(__file__).resolve().parent
DATA = golden.GOLDEN.parents[2]  # the test data checkout
REL = golden.GOLDEN.relative_to(DATA).as_posix()
LOG = golden.GOLDEN / "supervisor.log"
SETTINGS = golden.COLLATED / "step2_settings.json"
SHARD_RE = re.compile(r"(words|batches) shard (\d+)/(\d+)")
TOTAL_WORDS_STALL = 30 * 60


def log(message: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(DATA), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")  # fmt: skip
    if check and done.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()[-500:]}")
    return done.stdout


def script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RESEARCH / name), *args], cwd=ADDON_ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})  # fmt: skip


def cloud_branches() -> list[str]:
    return [b.strip() for b in git("branch", "-r").splitlines()
            if b.strip().startswith("origin/claude/")]  # fmt: skip


def sync() -> None:
    """This machine's new results committed, every cloud branch merged, main pushed."""
    git("fetch", "-q", "origin")
    git("add", "--", f"{REL}/results/*/*.json", f"{REL}/results/*/run_log_*.jsonl", check=False)
    if git("diff", "--cached", "--name-only").strip():
        git("commit", "-q", "-m", "japanese_note_ai_ops: kanjify golden set, results made here")
    for branch in cloud_branches():
        # -X ours: an item two runners both answered keeps the answer main has already
        done = subprocess.run(["git", "-C", str(DATA), "merge", "-q", "--no-edit", "-X", "ours",
                               branch], capture_output=True, text=True)  # fmt: skip
        if done.returncode:
            git("merge", "--abort", check=False)
            log(f"merge of {branch} failed, skipped this round: {done.stderr.strip()[-300:]}")
    git("merge", "-q", "--no-edit", "origin/main", check=False)
    push = subprocess.run(["git", "-C", str(DATA), "push", "-q", "origin", "HEAD:main"],
                          capture_output=True, text=True)  # fmt: skip
    if push.returncode:
        log(f"push failed: {push.stderr.strip()[-300:]}")


def branch_activity() -> dict[tuple[str, int, int], float]:
    """(queue, shard, of) -> when its branch last pushed results, from the commit subjects."""
    out: dict[tuple[str, int, int], float] = {}
    for branch in cloud_branches():
        for line in git("log", "-50", "--format=%ct %s", branch).splitlines():
            stamp, _, subject = line.partition(" ")
            m = SHARD_RE.search(subject)
            if m:
                key = (m[1], int(m[2]), int(m[3]))
                out[key] = max(out.get(key, 0.0), float(stamp))
    return out


def queue_items(name: str) -> list[dict]:
    return golden.read_jsonl(golden.QUEUES / f"{name}.jsonl")


def left(name: str, shard: Optional[tuple[int, int]] = None) -> list[str]:
    import agent_queue

    out = golden.RESULTS / name
    return [it["id"] for it in queue_items(name)
            if (shard is None or agent_queue.in_shard(it["id"], shard))
            and not (out / f"{it['id']}.json").exists()]  # fmt: skip


def f1(c: dict) -> float:
    right = c.get("right", 0)
    p = right / ((right + c.get("wrong", 0) + c.get("extra", 0)) or 1)
    r = right / ((right + c.get("wrong", 0) + c.get("missed", 0)) or 1)
    return 100 * 2 * p * r / ((p + r) or 1)


def choose_settings(args) -> Optional[dict]:
    """Step 2's batch size and effort from the pilot, by the rule the user approved; None until
    every pilot batch has answered."""
    import kanjify_golden_collate as collate

    items = queue_items("pilot_batch")
    if not items or left("pilot_batch"):
        return None
    sentences = {s["sid"]: s for s in golden.read_jsonl(golden.INVENTORY / "sentences.jsonl")}
    hand = golden.hand_labels(golden.read_sentences())
    figures: dict[str, dict] = {}
    for way, rows in (("single", collate.step2_rows("pilot_single", sentences)),
                      ("pilot_batch", collate.step2_rows("pilot_batch", sentences))):  # fmt: skip
        for row in rows:
            if row["problem"] or row["sid"] not in hand:
                continue
            key = way if way == "single" else row["labeller"].split("/")[1].split("_")[0]
            figures.setdefault(key, {"pairs": []})["pairs"].append(
                (row["sid"], row["sentence"], hand[row["sid"]]["kanjified"], row["kanjified"]))
    scores = {k: f1(collate.agreement(v["pairs"])[0]) for k, v in figures.items()}
    seconds: dict[str, list[float]] = {}
    for r in collate.read_results("pilot_batch"):
        size = r["id"].split("_")[0]
        seconds.setdefault(size, []).append(r.get("seconds") or 0)
    per_batch = {k: sum(v) / len(v) for k, v in seconds.items() if v}
    sentences_n = len(sentences) * 1.05  # the duplicates too
    now = dt.datetime.now()
    deadline = dt.datetime.combine(now.date() + dt.timedelta(days=now.hour >= 20),
                                   dt.time.fromisoformat(args.deadline))  # fmt: skip

    def fits(size: int, factor: float = 1.0) -> bool:
        batch_seconds = per_batch.get(f"batch{size}", per_batch.get("batch10", 600) * size / 10)
        hours = sentences_n / size * batch_seconds * factor / args.parallel / 3600
        return now + dt.timedelta(hours=hours) <= deadline

    s10, s30 = scores.get("batch10", 0.0), scores.get("batch30", 0.0)
    size = 30 if s30 >= s10 - 2 else (10 if fits(10) else 20)
    batch_score = s30 if size == 30 else s10
    effort = "high"
    if scores.get("single", 0.0) - batch_score > 5 and fits(size, 1.5):
        effort = "xhigh"
    return {"size": size, "effort": effort, "f1_vs_hand": scores, "seconds_per_batch": per_batch,
            "decided": now.isoformat(timespec="seconds")}  # fmt: skip


def published_at() -> Optional[float]:
    """When step 2's queue was committed, or None while it is not."""
    stamp = git("log", "-1", "--format=%ct", "--", f"{REL}/queues/batches.jsonl").strip()
    return float(stamp) if stamp and queue_items("batches") else None


def words_ready(state: dict) -> bool:
    n = len(queue_items("words")) - len(left("words"))
    total = len(queue_items("words"))
    if n != state.get("words_seen"):
        state["words_seen"], state["words_since"] = n, time.time()
    stalled = time.time() - state.get("words_since", time.time()) >= TOTAL_WORDS_STALL
    return n >= total or (n >= 0.95 * total and stalled)


def publish(settings: dict) -> None:
    if settings["size"] != 20:
        done = script("kanjify_golden_inventory.py", "--batch-size", str(settings["size"]))
        log(f"inventory at batch size {settings['size']}: exit {done.returncode}")
    script("kanjify_golden_collate.py")
    done = script("kanjify_golden_render.py", "batches", "--effort", settings["effort"])
    log(f"render batches: {done.stdout.strip()[-200:]} {done.stderr.strip()[-200:]}")
    git("add", "--", f"{REL}/queues/batches.jsonl", f"{REL}/inventory", f"{REL}/decisions.jsonl",
        str(SETTINGS.relative_to(DATA)))  # fmt: skip
    git("commit", "-q", "-m", "japanese_note_ai_ops: kanjify golden set, step 2 queue at batch"
        f" size {settings['size']}, effort {settings['effort']}")  # fmt: skip
    sync()


class Fallback:
    """One local agent_queue, on the items the idle shards have left, 3 workers in all: this
    machine's limit, however many shards stand still."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.key: Optional[frozenset] = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, keys: frozenset) -> None:
        (golden.RESULTS / "STOP").unlink(missing_ok=True)
        queue = next(iter(keys))[0]
        ids = sorted({i for _, s, n in keys for i in left(queue, (s, n))})
        log(f"fallback: working {len(ids)} {queue} items of shards {sorted(k[1] for k in keys)} here")
        self.proc = subprocess.Popen(
            [sys.executable, str(RESEARCH / "agent_queue.py"), str(golden.QUEUES / f"{queue}.jsonl"),
             "--workers", "3", "--wait-for-reset", "--only", *ids],
            cwd=ADDON_ROOT, stdout=(golden.GOLDEN / "fallback.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT, env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )  # fmt: skip
        self.key = keys

    def stop(self) -> None:
        if self.running():
            log(f"fallback: {self.key} is moving again in the cloud, stopping here")
            (golden.RESULTS / "STOP").write_text("supervisor")
            assert self.proc is not None
            self.proc.wait()
            (golden.RESULTS / "STOP").unlink(missing_ok=True)
        self.proc = None
        self.key = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--deadline", default="18:00", help="step 2 should finish by then")
    parser.add_argument("--parallel", type=int, default=32, help="subagents at once, in all")
    parser.add_argument("--idle", type=float, default=60, help="minutes before a shard's fallback")
    parser.add_argument("--interval", type=float, default=300, help="seconds between rounds")
    args = parser.parse_args()
    state: dict = {}
    started = time.time()
    fallback = Fallback()
    while True:
        try:
            sync()
            settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else None
            if settings is None:
                settings = choose_settings(args)
                if settings is not None:
                    golden.COLLATED.mkdir(parents=True, exist_ok=True)
                    SETTINGS.write_text(json.dumps(settings, indent=1) + "\n")
                    log(f"step 2 settings: {settings}")
            since = published_at()
            if settings is not None and since is None and words_ready(state):
                publish(settings)
                since = published_at()
            phase = "batches" if since is not None else "words"
            if since is not None and not left("batches"):
                fallback.stop()
                script("kanjify_golden_collate.py")
                git("add", "--", f"{REL}/collated", f"{REL}/decisions.jsonl", check=False)
                git("commit", "-q", "-m", "japanese_note_ai_ops: kanjify golden set, collated",
                    check=False)  # fmt: skip
                sync()
                log("every step 2 item has a result: done")
                return 0
            activity = branch_activity()
            now = time.time()
            idle = []
            for i in range(args.shards):
                key = (phase, i, args.shards)
                last = max(activity.get(key, 0.0), activity.get(("words", i, args.shards), 0.0),
                           since or started, started)  # fmt: skip
                if now - last > args.idle * 60 and left(phase, (i, args.shards)):
                    idle.append(key)
            if fallback.running() and fallback.key != frozenset(idle):
                fallback.stop()
            if not fallback.running() and idle:
                fallback.start(frozenset(idle))
            log(f"{phase}: words left {len(left('words'))}, batches left "
                f"{len(left('batches')) if queue_items('batches') else '-'}, idle shards "
                f"{[k[1] for k in idle]}, fallback {sorted(k[1] for k in fallback.key or ())}")  # fmt: skip
        except Exception as e:  # noqa: BLE001 - one bad round must not end the night's work
            log(f"round failed: {type(e).__name__}: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
