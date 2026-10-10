"""Export captured AI calls as JSONL, for eval sets and the research scripts' answer caches.

    python dev/export_evals.py --capture STORE --kind match.meanings [--run ID ...]
        [--model MODEL] [--outcome ok] [--out FILE]

One row per call, newest last: `key`, `model` and `response` as the research scripts' caches
hold them (`key` is capture_store.research_cache_key, sha1 of model and prompt, so a row seeds
`judge_eval`'s cache and its siblings as is), and beside them what an eval set is made from:
the call's `kind`, `request_key` (the same case across prompt rewordings), `inputs` (the values
the prompt was built from, to render a new prompt version from), `prompt`, `outcome`, and where
it came from (`run_id`, `call_id`, `note_id`). Editing the responses into gold answers, and
keeping them, is done in the file: the store prunes old runs, a JSONL file is diffed and kept.

Writes to stdout unless `--out` is given. Reads the store only; needs neither anki nor Anki.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Iterator, Optional, Sequence

CAPTURE_STORE = Path(__file__).resolve().parents[1] / "async_api_ops" / "capture_store.py"


def _capture_store() -> ModuleType:
    """capture_store on its own, from its file: it imports nothing of the addon's, so the keys
    are the store's own without importing anki to reach them."""
    spec = importlib.util.spec_from_file_location("jnaio_capture_store", CAPTURE_STORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows(
    store: Path,
    kinds: Sequence[str],
    run_ids: Sequence[int] = (),
    model: Optional[str] = None,
    outcome: Optional[str] = "ok",
) -> Iterator[dict]:
    """The calls of `kinds` (of `run_ids`, of `model`, with `outcome`, when given), as rows."""
    research_cache_key = _capture_store().research_cache_key
    where = [f"kind IN ({','.join('?' * len(kinds))})"]
    params: list = list(kinds)
    if run_ids:
        where.append(f"run_id IN ({','.join('?' * len(run_ids))})")
        params += list(run_ids)
    if model is not None:
        where.append("model = ?")
        params.append(model)
    if outcome is not None:
        where.append("outcome = ?")
        params.append(outcome)
    query = (
        "SELECT call_id, run_id, note_id, kind, request_key, model, prompt, inputs_json,"
        f" response_json, outcome FROM calls WHERE {' AND '.join(where)} ORDER BY call_id"
    )
    with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as connection:
        for row in connection.execute(query, params):
            call_id, run_id, note_id, kind, request_key, row_model, prompt, inputs, response, out = row
            yield {
                "key": research_cache_key(row_model, prompt),
                "model": row_model,
                "response": json.loads(response) if response else None,
                "kind": kind,
                "request_key": request_key,
                "inputs": json.loads(inputs) if inputs else None,
                "prompt": prompt,
                "outcome": out,
                "run_id": run_id,
                "call_id": call_id,
                "note_id": note_id,
            }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--kind", action="append", required=True, dest="kinds")
    parser.add_argument("--run", action="append", type=int, default=[], dest="run_ids")
    parser.add_argument("--model")
    parser.add_argument("--outcome", default="ok", help="'any' for every outcome")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    outcome = None if args.outcome == "any" else args.outcome
    lines = [
        json.dumps(row, ensure_ascii=False)
        for row in rows(args.capture, args.kinds, args.run_ids, args.model, outcome)
    ]
    text = "\n".join(lines) + ("\n" if lines else "")
    if args.out is None:
        sys.stdout.buffer.write(text.encode("utf-8"))
    else:
        args.out.write_text(text, encoding="utf-8")
        print(f"{len(lines)} rows to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
