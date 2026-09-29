"""A queue's items for a Claude Code session that runs them as its own subagents.

`agent_queue.py` runs each item as a headless `claude -p`. A cloud session may have no
logged-in CLI to start, and its own subagents are what it has plenty of: its lead takes items
with this script, hands each to a subagent (the agent types in `.claude/agents/`, which fix the
model, effort and tools an item asks for), and saves each answer with it. The result file is
the one `agent_queue.py` writes, so collate reads both alike, and either runner resumes the
other's queue.

    python word_array/research/agent_items.py take QUEUE [--shard I/N] [--count K] [--out DIR]
    python word_array/research/agent_items.py save QUEUE ID ANSWER_FILE
    python word_array/research/agent_items.py status QUEUE... [--shard I/N]

`take` writes the next K prompts of the shard that have no result and no claim (a claim file
the session makes for each item it hands out, so two of its subagents never get one item) to
DIR (default `output/agent_items/<queue>/`, gitignored, with the claims: neither belongs in the
test data repo), each followed by its output schema since no CLI enforces it here, and prints one line per item: id, agent type, prompt file. `{PYTHON}` becomes
`python` and the lookup's path starts from the repo root. `save` takes the answer as the subagent's final message saved to a file, keeps the
last JSON object in it, checks the schema's required keys, writes `<id>.json` and drops the
claim; for a batch it also prints any row that fails `kanjify_golden.row_problem`. A claim
older than `--stale` minutes (default 90) counts as abandoned: its subagent died with its
session.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from _bootstrap import OUTPUT

import agent_queue
import kanjify_golden as golden

CLAIM = ".claim"
# A subagent starts where its session does, the repo root, so the lookup runs by its full path;
# the agent types allow that one command and nothing else
LOOKUP_FROM_ROOT = "python japanese_note_ai_ops/word_array/"


def agent_type(item: dict) -> str:
    """The `.claude/agents/` type that runs an item: its model, effort and tools fixed there, as
    `agent_queue.command` sets them on the command line."""
    if item.get("schema") == "word_schema.json":
        return "kanjify-word"
    if item.get("tools") == "lookup+web":
        return "kanjify-single"
    return f"kanjify-batch-{item.get('effort') or 'high'}"


def queue_path(name: str) -> Path:
    path = Path(name)
    return path if path.suffix == ".jsonl" else golden.QUEUES / f"{name}.jsonl"


def read_queue(path: Path) -> list[dict]:
    return golden.read_jsonl(path)


def out_dir(path: Path) -> Path:
    return golden.RESULTS / path.stem


def work_dir(path: Path) -> Path:
    """The session's prompt files and claims: this machine's, never committed."""
    work = OUTPUT / "agent_items" / path.stem
    work.mkdir(parents=True, exist_ok=True)
    return work


def schema_text(item: dict) -> str:
    schema = item.get("schema")
    if not schema:
        return ""
    text = (agent_queue.AGENTS_DIR / schema).read_text(encoding="utf-8")
    return (
        "\n\n## Output schema\n\nYour final message is one JSON object valid against this schema,"
        " with nothing before or after it:\n\n```json\n" + text.strip() + "\n```\n"
    )


def claimed(work: Path, item_id: str, stale_minutes: float) -> bool:
    claim = work / f"{item_id}{CLAIM}"
    return claim.exists() and time.time() - claim.stat().st_mtime < stale_minutes * 60


def take(args) -> int:
    path = queue_path(args.queue)
    out = out_dir(path)
    work = work_dir(path)
    prompts = args.out or work
    prompts.mkdir(parents=True, exist_ok=True)
    taken = 0
    for item in read_queue(path):
        if taken >= args.count:
            break
        if not agent_queue.in_shard(item["id"], args.shard):
            continue
        if (out / f"{item['id']}.json").exists() or claimed(work, item["id"], args.stale):
            continue
        prompt = item["prompt"].replace("{PYTHON} word_array/", LOOKUP_FROM_ROOT)
        prompt = agent_queue.fill(prompt, "python") + schema_text(item)
        file = prompts / f"{item['id']}.md"
        file.write_text(prompt, encoding="utf-8")
        (work / f"{item['id']}{CLAIM}").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
        print(f"{item['id']}\t{agent_type(item)}\t{file}")
        taken += 1
    if not taken:
        print("nothing left to take in this shard")
    return 0


def check(item: dict, answer: dict) -> Optional[str]:
    schema = item.get("schema")
    if not schema:
        return None
    required = json.loads((agent_queue.AGENTS_DIR / schema).read_text(encoding="utf-8"))["required"]
    missing = [key for key in required if key not in answer]
    return f"missing {', '.join(missing)}" if missing else None


def save(args) -> int:
    path = queue_path(args.queue)
    out = out_dir(path)
    items = {it["id"]: it for it in read_queue(path)}
    item = items.get(args.id)
    if item is None:
        print(f"no item {args.id} in {path.name}")
        return 1
    text = Path(args.answer).read_text(encoding="utf-8")
    answer = agent_queue.terminal_client.last_json_object(text)
    if answer is None:
        print(f"{args.id}: no JSON object in the answer; ask the subagent again")
        return 1
    problem = check(item, answer)
    if problem:
        print(f"{args.id}: {problem}; ask the subagent again")
        return 1
    result = {
        "id": item["id"],
        "structured": answer if item.get("schema") else None,
        "text": text,
        "cost": None,  # a subagent's cost is not reported to its lead
        "seconds": args.seconds,
        "usage": None,
        "model": item.get("model"),
        "effort": item.get("effort"),
        "runner": "subagent",
        "meta": item.get("meta", {}),
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{item['id']}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(target)
    (work_dir(path) / f"{item['id']}{CLAIM}").unlink(missing_ok=True)
    note = ""
    if item.get("schema") == "batch_schema.json":
        sentences = {s["sid"]: s for s in golden.read_jsonl(golden.INVENTORY / "sentences.jsonl")}
        for row in answer.get("rows", []):
            source = sentences.get(row.get("sid"))
            if source is None:
                note += f"\n  {row.get('sid')}: not a sentence of this item"
                continue
            problem = golden.row_problem(source["sentence"], row.get("kanjified", ""))
            if problem:
                note += f"\n  {row['sid']}: {problem.splitlines()[0]}"
    print(f"saved {target.name}" + (f"; rows that will be rejected:{note}" if note else ""))
    return 0


def status(args) -> int:
    for name in args.queues:
        path = queue_path(name)
        out = out_dir(path)
        items = [it for it in read_queue(path) if agent_queue.in_shard(it["id"], args.shard)]
        done = sum((out / f"{it['id']}.json").exists() for it in items)
        work = work_dir(path)
        running = sum(claimed(work, it["id"], args.stale) for it in items)
        print(f"{path.stem}: {done} done, {running} claimed, {len(items) - done} left of "
              f"{len(items)} in shard {args.shard[0]}/{args.shard[1]}")  # fmt: skip
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("take")
    p.add_argument("queue")
    p.add_argument("--shard", type=agent_queue.parse_shard, default=(0, 1))
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--out", type=Path)
    p.add_argument("--stale", type=float, default=90.0)
    p = sub.add_parser("save")
    p.add_argument("queue")
    p.add_argument("id")
    p.add_argument("answer")
    p.add_argument("--seconds", type=float)
    p = sub.add_parser("status")
    p.add_argument("queues", nargs="+")
    p.add_argument("--shard", type=agent_queue.parse_shard, default=(0, 1))
    p.add_argument("--stale", type=float, default=90.0)
    args = parser.parse_args()
    return {"take": take, "save": save, "status": status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
