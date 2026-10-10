"""Run one transcriber over the furigana audio eval's picked clips and keep what it heard.

Each run writes `evals/furigana_audio/runs/<model>.jsonl` in the test data checkout, one row per
card: its id, the text the model gave and the seconds it took; `<model>.meta.json` beside it
says which revision of the model, with which settings, on what. Cards already in the file are
skipped, so a run that stops is started again and goes on where it was. While a run goes on,
its rows go to `<model>.jsonl.part`, which the test data repo ignores, and into the run file
when it ends: the tracked file changes once a run, not with every card, and a part a stopped
run leaves is picked up by the next.

The models, their settings and their loaders are the op's transcription server's
(`dev/transcribe_server.py`, which lists them), each pinned to the revision these runs measured,
so the eval hears what the op will. Like the server, this runs in an environment of its own,
set up as that file says:

    python word_array/research/furigana_audio_run.py MODEL [--limit N] [--stratum S]
        [--device cuda]

`--stratum` takes the cards of one stratum only (`inline` is the 40 whose hard word the line
reads itself): Ruby-ASR's decoder takes over a minute a card on a 4-core CPU.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

import furigana_audio as fa
from _bootstrap import transcribe_server

if TYPE_CHECKING:
    # The server is loaded by its path at run time (`_bootstrap.transcribe_server`); dev/ is on
    # mypy's path, so the real module is named for type checking only
    import transcribe_server as ts
    from transcribe_server import Model
else:
    ts = transcribe_server()
    Model = ts.Model

MODELS = ts.MODELS
RUNS = fa.HOME / "runs"


def meta(name: str, model: Model, device: str) -> dict:
    hub = ts.module("huggingface_hub")
    return {
        "model": name,
        "repo": model.repo,
        "revision": hub.model_info(model.repo, revision=model.revision).sha,
        "kind": model.kind,
        "generate": model.generate,
        "device": device,
        "torch": ts.module("torch").__version__,
        "transformers": ts.module("transformers").__version__,
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run(name: str, limit: Optional[int], device: str, stratum: Optional[str] = None) -> int:
    model = MODELS[name]
    selection = [r for r in fa.read_jsonl(fa.SELECTION) if stratum in (None, r["stratum"])]
    if not selection:
        print(f"{fa.SELECTION} is missing or empty: run furigana_audio_select.py", file=sys.stderr)
        return 2
    out = RUNS / f"{name}.jsonl"
    part = RUNS / f"{name}.jsonl.part"
    done = {row["id"] for row in fa.read_jsonl(out) + fa.read_jsonl(part)}
    todo = [row for row in selection if row["id"] not in done]
    if limit:
        todo = todo[:limit]
    if not todo:
        fold(out, part)
        print(f"{out}: every picked card is done")
        return 0
    missing = [row["audio"] for row in todo if not (fa.AUDIO / row["audio"]).exists()]
    if missing:
        print(f"{len(missing)} clips are not in {fa.AUDIO}: {missing[:3]}", file=sys.stderr)
        return 2
    if device == "cpu":
        ts.module("torch").set_num_threads(os.cpu_count() or 1)
    transcribe = ts.load(name, device)
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{name}.meta.json").write_text(
        json.dumps(meta(name, model, device), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with part.open("a", encoding="utf-8", newline="\n") as f:
        for n, row in enumerate(todo, 1):
            wav = ts.load_clip(fa.AUDIO / row["audio"])
            start = time.perf_counter()
            text = transcribe(wav)
            seconds = round(time.perf_counter() - start, 2)
            record = {"id": row["id"], "text": text, "seconds": seconds}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            print(f"{n}/{len(todo)} {row['id']} {seconds}s {text[:60]}", flush=True)
    fold(out, part)
    return 0


def fold(out: Path, part: Path) -> None:
    """The part's rows into the run file, every row in the selection's order."""
    if not part.exists():
        return
    order = {row["id"]: n for n, row in enumerate(fa.read_jsonl(fa.SELECTION))}
    rows = {row["id"]: row for row in fa.read_jsonl(out) + fa.read_jsonl(part)}
    fa.write_jsonl(out, sorted(rows.values(), key=lambda row: order.get(row["id"], len(order))))
    part.unlink()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("model", choices=sorted(MODELS))
    ap.add_argument("--limit", type=int, help="at most this many cards this run")
    ap.add_argument("--stratum", help="only the cards of this stratum (names, readings, ...)")
    ap.add_argument("--device", default="cpu", help='"cpu" (default) or "cuda"')
    args = ap.parse_args(argv)
    return run(args.model, args.limit, args.device, args.stratum)


if __name__ == "__main__":
    sys.exit(main())
