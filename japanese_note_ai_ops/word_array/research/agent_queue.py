"""Runs a queue of rendered agent prompts through headless `claude -p`, one result file per item.

A queue is a JSONL file, one item per line: `{"id", "prompt", "model", "effort", "tools",
"schema", ...}` (`kanjify_golden_render.py` writes them). Each item runs as

    claude -p --model MODEL --effort EFFORT --output-format json [--json-schema SCHEMA]
           --tools TOOLS --allowed-tools RULES --permission-mode dontAsk
           --no-session-persistence --safe-mode --strict-mcp-config

with the prompt on stdin (Japanese on a Windows command line is mangled), from the addon
directory. `tools` names a set: `none`, `lookup` (Bash, allowed only the read-only
`kanjify_lookup.py`) or `lookup+web` (that and WebSearch, WebFetch). Nothing an agent can run
writes anything; the driver writes its answer.

    py -3.10 word_array/research/agent_queue.py QUEUE [QUEUE...] [--out DIR] [--workers 3]
        [--shard I/N] [--limit N] [--dry-run] [--wait-for-reset] [--max-load 85]

- Several queues share the workers, taken in the order given: the first queue's items first,
  or with `--interleave` one item of each in turn.
- One result file per item, `<out>/<queue name>/<id>.json` (default out: `results/` beside the
  queues' directory): the answer (`structured`, the schema's object, else `text`), cost, usage,
  seconds, and the item's `meta`. An item with a result file is skipped, so a rerun resumes.
  An item that failed leaves `<id>.error.json` and is tried again on the next run.
- `--shard I/N` takes the items whose id hashes to I of N, so N machines split one queue
  without talking to each other; each writes only its own items' files.
- Each finished item appends a line to its queue's `run_log_<shard>.jsonl`: id, outcome, cost,
  seconds. The CLI's `total_cost_usd` is what the item would cost at API prices; on a
  subscription it measures the share of the usage limit it took.
- The subscription's usage limit stops the queue cleanly: nothing new starts, what runs
  finishes, and the item that hit the limit is left for the next run. `--wait-for-reset`
  sleeps until the reset time the CLI states instead, then goes on. An expired login or an
  unusable model stops it at once.
- A file named STOP in the out directory stops it the same way, from another terminal or
  machine; Ctrl+C too (a second one kills the running agents).
- `--max-load` starts a new agent only while the CPU is under that percent (with psutil):
  a few `claude` processes at once already load a 4-core machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from _bootstrap import ADDON_ROOT, load_root

terminal_client = load_root("async_api_ops.terminal_client")

AGENTS_DIR = Path(__file__).resolve().parent / "kanjify_agents"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_TIMEOUT = 60 * 60
LOOKUP = "word_array/research/kanjify_lookup.py"
TOOL_SETS = {
    "none": ([], []),
    "lookup": (["Bash"], ["Bash({python} " + LOOKUP + ":*)"]),
    "lookup+web": (
        ["Bash", "WebSearch", "WebFetch"],
        ["Bash({python} " + LOOKUP + ":*)", "WebSearch", "WebFetch"],
    ),
}


def default_python() -> str:
    """The command agents run the lookups with, as they must type it: the Windows launcher's
    `py -3.10` where the dev interpreter is reached that way, else `python3`."""
    return "py -3.10" if os.name == "nt" else "python3"


def in_shard(item_id: str, shard: tuple[int, int]) -> bool:
    i, n = shard
    return int(hashlib.sha1(item_id.encode("utf-8")).hexdigest(), 16) % n == i


def parse_shard(text: str) -> tuple[int, int]:
    i, n = (int(x) for x in text.split("/"))
    if not 0 <= i < n:
        raise argparse.ArgumentTypeError("shard I/N needs 0 <= I < N")
    return i, n


def fill(prompt: str, python: str) -> str:
    """Placeholders a queue leaves for the machine that runs it."""
    return prompt.replace("{PYTHON}", python)


def command(item: dict, claude: str, python: str) -> list[str]:
    tools, rules = TOOL_SETS[item.get("tools", "none")]
    cmd = [claude, "-p", "--model", item.get("model") or DEFAULT_MODEL]
    if item.get("effort"):
        cmd += ["--effort", item["effort"]]
    cmd += ["--output-format", "json", "--tools", ",".join(tools)]
    if rules:
        cmd += ["--allowed-tools", *[r.format(python=python) for r in rules]]
    cmd += ["--permission-mode", "dontAsk", "--no-session-persistence", "--safe-mode"]
    cmd += ["--strict-mcp-config"]
    schema = item.get("schema")
    if schema:
        text = (AGENTS_DIR / schema).read_text(encoding="utf-8")
        cmd += ["--json-schema", json.dumps(json.loads(text), separators=(",", ":"))]
    return cmd


class Queue:
    """What the workers share: the items left, the stop flag and the totals."""

    def __init__(self, items: list[dict], out: Path, args):
        self.items = items
        self.out = out
        self.args = args
        self.lock = threading.Lock()
        self.stop_reason: Optional[str] = None
        self.resume_at: Optional[float] = None
        self.cost = 0.0
        self.done = 0
        self.failed = 0
        self.procs: set[subprocess.Popen] = set()
        self.log_name = f"run_log_{args.shard[0]}of{args.shard[1]}.jsonl"

    def stop(self, reason: str, resume_at: Optional[float] = None) -> None:
        with self.lock:
            if self.stop_reason is None:
                self.stop_reason = reason
                print(f"stopping: {reason}", flush=True)
            if resume_at is not None:
                self.resume_at = max(self.resume_at or 0, resume_at)

    def stopped(self) -> bool:
        if (self.out / "STOP").exists():
            self.stop("STOP file")
        return self.stop_reason is not None

    def next_item(self) -> Optional[dict]:
        with self.lock:
            while self.items:
                item = self.items.pop(0)
                if not (item["_out"] / f"{item['id']}.json").exists():
                    return item
        return None

    def record(self, item: dict, row: dict) -> None:
        with self.lock:
            with (item["_out"] / self.log_name).open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def cpu_ok(max_load: float) -> bool:
    try:
        import psutil  # type: ignore
    except ImportError:
        return True
    return psutil.cpu_percent(interval=1.0) < max_load


def run_item(queue: Queue, item: dict, claude: str, python: str) -> None:
    args = queue.args
    cmd = command(item, claude, python)
    prompt = fill(item["prompt"], python)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for attempt in range(1, args.retries + 2):
        started = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=ADDON_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        with queue.lock:
            queue.procs.add(proc)
        try:
            out, err = proc.communicate(prompt.encode("utf-8"), timeout=args.timeout)
            exit_code: Optional[int] = proc.returncode
        except subprocess.TimeoutExpired:
            terminal_client.kill_process_tree(proc)
            out, err = proc.communicate()
            exit_code = None
        finally:
            with queue.lock:
                queue.procs.discard(proc)
        seconds = round(time.time() - started, 1)
        stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        outcome = terminal_client.classify_result(exit_code, stdout, stderr)
        cost = (outcome.usage or {}).get("total_cost_usd") or 0.0
        with queue.lock:
            queue.cost += cost
        log = {"id": item["id"], "attempt": attempt, "action": outcome.action, "cost": cost,
               "seconds": seconds, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}  # fmt: skip
        action = outcome.action
        if action == terminal_client.CliAction.OK:
            structured = outcome.result
            if structured is None and item.get("schema"):
                structured = terminal_client.last_json_object(outcome.message)
            if structured is None and item.get("schema"):
                action = "unparsed"
            else:
                result = {
                    "id": item["id"],
                    "structured": structured,
                    "text": outcome.message,
                    "cost": cost,
                    "seconds": seconds,
                    "usage": outcome.usage,
                    "model": item.get("model") or DEFAULT_MODEL,
                    "effort": item.get("effort"),
                    "meta": item.get("meta", {}),
                    "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                path = item["_out"] / f"{item['id']}.json"
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(path)
                (item["_out"] / f"{item['id']}.error.json").unlink(missing_ok=True)
                queue.record(item, log)
                with queue.lock:
                    queue.done += 1
                    done, total = queue.done, queue.cost
                print(f"ok   {item['id']} ${cost:.2f} {seconds:.0f}s (done {done}, ${total:.2f})",
                      flush=True)  # fmt: skip
                return
        log["action"] = action
        log["message"] = outcome.message[-500:]
        queue.record(item, log)
        if action == terminal_client.CliAction.EXHAUSTED:
            resume = terminal_client.usage_limit_resume_time(outcome.message, time.time())
            queue.stop(f"usage limit: {outcome.message.strip()[:200]}", resume)
            return
        if action in terminal_client.STOP_REASONS:
            queue.stop(terminal_client.STOP_REASONS[action])
            return
        retry = action in (terminal_client.CliAction.RETRY, "unparsed") or exit_code is None
        print(f"fail {item['id']} {action} (attempt {attempt}): {outcome.message.strip()[:200]}",
              flush=True)  # fmt: skip
        if not retry or queue.stopped():
            break
        time.sleep(10 * attempt)
    error = {"id": item["id"], "action": action, "message": outcome.message[-2000:],
             "stderr": stderr[-2000:], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}  # fmt: skip
    (item["_out"] / f"{item['id']}.error.json").write_text(
        json.dumps(error, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    with queue.lock:
        queue.failed += 1


def worker(queue: Queue, claude: str, python: str) -> None:
    while not queue.stopped():
        while not cpu_ok(queue.args.max_load):
            if queue.stopped():
                return
            time.sleep(5)
        item = queue.next_item()
        if item is None:
            return
        run_item(queue, item, claude, python)


def run(queue: Queue, claude: str, python: str) -> None:
    threads = []
    for n in range(queue.args.workers):
        t = threading.Thread(target=worker, args=(queue, claude, python), daemon=True)
        t.start()
        threads.append(t)
        # staggered, so the CPU check sees the ones already started
        time.sleep(0 if n + 1 == queue.args.workers else 15)
    for t in threads:
        while t.is_alive():
            t.join(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("queues", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, help="default: ../results/ beside the queues")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--shard", type=parse_shard, default=(0, 1), help="I/N")
    parser.add_argument("--limit", type=int, default=0, help="run at most N items")
    parser.add_argument("--only", nargs="*", default=[], help="only these item ids")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--interleave", action="store_true", help="one item of each queue in turn")
    parser.add_argument("--wait-for-reset", action="store_true")
    parser.add_argument("--max-load", type=float, default=85.0)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per item")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--python", default=default_python(), help="how agents run a script")
    args = parser.parse_args()

    out = args.out or args.queues[0].parent.parent / "results"
    per_queue = []
    for path in args.queues:
        (out / path.stem).mkdir(parents=True, exist_ok=True)
        with path.open(encoding="utf-8") as f:
            queued = [dict(json.loads(line), _out=out / path.stem) for line in f if line.strip()]
        print(f"{len(queued)} items in {path.name}")
        per_queue.append(queued)
    if args.interleave:
        longest = max(len(q) for q in per_queue)
        all_items = [q[i] for i in range(longest) for q in per_queue if i < len(q)]
    else:
        all_items = [it for q in per_queue for it in q]

    def left() -> list[dict]:
        return [
            it
            for it in all_items
            if in_shard(it["id"], args.shard)
            and (not args.only or it["id"] in args.only)
            and not (it["_out"] / f"{it['id']}.json").exists()
        ]

    items = left()
    if args.limit:
        items = items[: args.limit]
    claude = terminal_client.find_cli({}) or "claude"
    print(f"{len(items)} to run in shard {args.shard[0]}/{args.shard[1]} -> {out}")
    if args.dry_run:
        for it in items[:20]:
            print(f"  {it['id']} {it.get('effort')} {it.get('tools')} {len(it['prompt'])} chars")
        if items:
            shown = command(items[0], claude, args.python)
            if "--json-schema" in shown:
                shown[shown.index("--json-schema") + 1] = "<schema>"
            print("command:", " ".join(shown))
        return 0

    queue = Queue(items, out, args)

    def on_interrupt(signum, frame):
        if queue.stop_reason is None:
            queue.stop("interrupted; Ctrl+C again kills the running agents")
        else:
            with queue.lock:
                procs = list(queue.procs)
            for proc in procs:
                terminal_client.kill_process_tree(proc)

    signal.signal(signal.SIGINT, on_interrupt)
    while True:
        run(queue, claude, args.python)
        print(f"done {queue.done}, failed {queue.failed}, cost ${queue.cost:.2f}")
        if not (args.wait_for_reset and queue.resume_at and queue.stop_reason
                and queue.stop_reason.startswith("usage limit")):  # fmt: skip
            break
        wait = max(60.0, queue.resume_at - time.time() + 60)
        print(f"waiting {wait / 60:.0f} min for the usage limit to reset", flush=True)
        time.sleep(wait)
        queue.stop_reason = None
        queue.resume_at = None
        queue.items = [it for it in left() if it in items]
    return 0 if queue.stop_reason is None or queue.stop_reason == "STOP file" else 2


if __name__ == "__main__":
    sys.exit(main())
