"""The arbiter (`word_array/furigana_arbiter.py`) over the furigana audio eval's cards: the words
the op's rule leaves for review on the pair's runs, asked of each model as the op asks, and what
it would write scored against the labels by ear.

    py -3.10 word_array/research/furigana_audio_arbiter.py [--model M ...] [--workers 8]
        [-n COUNT] [--score-only] [--list]

Each card's furigana is the op's (`audio_furigana`: the show's names and words read from the
export's captions and names.jsonl, as the op reads them from the show's notes and
show_names.json; the transcripts kana-anime-whisper's and ruby-mora's runs), the prompt is the
op's (`furigana_arbiter.review_inputs`, the English subtitle as the translation), and each model
is asked through the op's request code (`base_ops.get_response`), so a `terminal-` model runs
`claude -p` as the op does. Answers are cached by model and prompt in
`evals/furigana_audio/arbiter_results.jsonl` of the test data checkout: a rerun pays only for
prompts that changed. `--workers` is requests at once, and for `terminal-` models the cap on
live `claude` processes.

Each model is scored alone, and the models given together as the op's `furigana_models` list
are (`agreed_reading`: every one sure of the same reading, heard at the word, keeping its kana).
Of the words in review: how many the arbiter writes, and of those labelled by ear how many right
and wrong. The words in review for a mismatch have no labels, so for them it counts the readings
written that are and are not the draft. `--list` lists, for the models together, every reading
written that is wrong or, unlabelled, not the draft, and every labelled word left in review.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Optional, Sequence

import furigana_audio as fa
import furigana_audio_score as score
import furigana_audio_select as sel
from _bootstrap import ADDON_ROOT, load, load_root
from furigana_audio_run import RUNS

audio_furigana = load("audio_furigana")
arbiter = load("furigana_arbiter")
capture_store = load_root("async_api_ops.capture_store")

RESULTS = fa.HOME / "arbiter_results.jsonl"
PAIR = ("kana-anime-whisper", "ruby-mora")
DEFAULT_MODELS = ("terminal-claude-haiku-5-5", "terminal-claude-sonnet-5-5")
KIND = "furigana_arbiter.note"
# The op's rule: a reading one transcript hears is written (set_furigana.QUORUM)
QUORUM = 1



class Card:
    def __init__(self, row: dict, furigana, inputs: dict) -> None:
        self.row = row
        self.furigana = furigana
        self.inputs = inputs
        self.prompt = arbiter.review_prompt(**inputs)


def read_cards() -> list[Card]:
    """The eval's cards with a word in review, as the op leaves them."""
    rows, _ = fa.read_export(fa.EXPORT)
    texts = [row.jp for row in rows]
    base = fa.captions.ReadingBase(texts, sel.jmdict_readings)
    names, words = fa.captions.show_readings(texts, base)
    user = fa.read_names()
    names.update(user)
    words -= set(user)
    tokenize = sel.sudachi_tokenizer()
    heard = {m: {r["id"]: r["text"] for r in fa.read_jsonl(RUNS / f"{m}.jsonl")} for m in PAIR}
    cards = []
    for row in fa.read_jsonl(fa.SELECTION):
        if any(not line for line in row["lines"]):
            continue  # a line the labelling emptied: no note holds that caption
        caption = "<br>".join(row["lines"])
        transcripts = [(m, heard[m][row["id"]]) for m in PAIR]
        furigana = audio_furigana.audio_furigana(
            caption,
            [text for _, text in transcripts],
            tokenize,
            names,
            sel.jmdict_readings,
            base=base,
            show_words=words,
            quorum=QUORUM,
        )
        if furigana.review:
            translation = arbiter.translation_text(row["en"])
            inputs = arbiter.review_inputs(caption, translation, transcripts, furigana)
            cards.append(Card(row, furigana, inputs))
    return cards


def load_get_response(workers: int):
    """The op's `get_response`, loaded with Anki stubbed and the add-on's config behind `mw`."""
    sys.path.insert(0, str(ADDON_ROOT / "test"))
    from addon_modules import load_ops_module, mw

    config = json.loads((ADDON_ROOT / "config.json").read_text(encoding="utf-8"))
    meta = ADDON_ROOT / "meta.json"
    if meta.exists():
        config.update(json.loads(meta.read_text(encoding="utf-8")).get("config", {}))
    # Here --workers is the cap on live `claude` processes, over every model of the run
    config["terminal_max_concurrent_requests"] = workers
    setattr(mw, "addonManager", SimpleNamespace(getConfig=lambda _name: config))
    return load_ops_module("base_ops").get_response


def read_results() -> dict[str, dict]:
    return {row["key"]: row for row in fa.read_jsonl(RESULTS)}


def fetch(get_response, model: str, todo: list[Card], workers: int, cached: dict) -> None:
    lock = threading.Lock()

    def ask(card: Card):
        return card, get_response(
            model,
            card.prompt,
            response_schema=arbiter.RESPONSE_SCHEMA,
            kind=KIND,
            inputs=card.inputs,
        )

    started, failed = time.monotonic(), 0
    with RESULTS.open("a", encoding="utf-8") as f, ThreadPoolExecutor(workers) as pool:
        for done, (card, response) in enumerate(pool.map(ask, todo), 1):
            if not isinstance(response, dict):
                failed += 1
                continue  # not cached, so a rerun asks again
            key = capture_store.research_cache_key(model, card.prompt)
            row = {"key": key, "model": model, "id": card.row["id"], "response": response}
            with lock:
                cached[key] = row
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
            if done % 25 == 0:
                print(f"  {model} ...{done}", file=sys.stderr)
    minutes = (time.monotonic() - started) / 60
    print(f"{model}: {len(todo)} requests ({failed} failed) in {minutes:.1f} min", file=sys.stderr)


def evaluate(
    models: Sequence[str], cards: list[Card], cached: dict, labels: dict, listing: bool
) -> list[str]:
    """The op's arbiter with `models`, all of them agreeing, over the cards."""
    c: Counter = Counter()
    listed: list[str] = []
    for card in cards:
        review = card.furigana.review
        answers = []
        for model in models:
            row = cached.get(capture_store.research_cache_key(model, card.prompt))
            c["unanswered cards"] += row is None
            answers.append(
                arbiter.answers_from_response(row["response"], len(review)) if row else {}
            )
        left = False
        for n, r in enumerate(review, 1):
            label = fa.label_for(labels, (card.row["id"], r.line, r.start), r.surface)
            said = label["reading"] if label and label["verdict"] == "heard" else None
            kind = "mismatch" if r.why == "mismatch" else "unsettled"
            c[f"{kind}: words"] += 1
            written = arbiter.agreed_reading(r, [by_model.get(n) for by_model in answers])
            if written is None:
                left = True
                if listing and said is not None:
                    given = ", ".join(
                        f"{a.reading}{'' if a.sure else '?'}" if a else "-"
                        for a in (by_model.get(n) for by_model in answers)
                    )
                    listed.append(
                        f"  left  {r.surface} {kind}: draft {r.draft}, said {said}, answers"
                        f" {given}, heard {'/'.join(r.heard) or '-'}  [{card.row['id']}]"
                    )
                continue
            c[f"{kind}: written"] += 1
            if said is not None:
                right = score.same_reading(written, said)
                c[f"{kind}: right" if right else f"{kind}: WRONG"] += 1
                worth_listing = not right
            else:
                as_draft = score.same_reading(written, r.draft)
                c[f"{kind}: unlabelled, {'draft' if as_draft else 'other'}"] += 1
                worth_listing = not as_draft
            if listing and worth_listing:
                mark = "WRONG" if said is not None else "other"
                text = card.furigana.texts[r.line]
                listed.append(
                    f"  {mark} {r.surface} {kind}: draft {r.draft}, said {said or '?'}, written"
                    f" {written}, heard {'/'.join(r.heard) or '-'} |"
                    f" {text[: r.start]}［{r.surface}］{text[r.end :]}  [{card.row['id']}]"
                )
        c["cards left with a review"] += left
    written = c["unsettled: written"] + c["mismatch: written"]
    words = c["unsettled: words"] + c["mismatch: words"]
    lines = [
        f"{' + '.join(models)}: written {written} of {words}, cards left with a review"
        f" {c['cards left with a review']} of {len(cards)}"
        + (f", {c['unanswered cards']} unanswered" if c["unanswered cards"] else ""),
        f"  unsettled: written {c['unsettled: written']} of {c['unsettled: words']}, labelled"
        f" right {c['unsettled: right']} WRONG {c['unsettled: WRONG']}, unlabelled"
        f" {c['unsettled: unlabelled, draft']} as the draft, {c['unsettled: unlabelled, other']}"
        f" other",
        f"  mismatch:  written {c['mismatch: written']} of {c['mismatch: words']}, labelled"
        f" right {c['mismatch: right']} WRONG {c['mismatch: WRONG']}, unlabelled"
        f" {c['mismatch: unlabelled, draft']} as the draft, {c['mismatch: unlabelled, other']}"
        f" other",
    ]
    return lines + listed


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", action="append", default=[], help="a model to ask (repeatable)")
    ap.add_argument("--workers", type=int, default=8, help="requests at once (default 8)")
    ap.add_argument("-n", type=int, default=0, help="only the first COUNT cards")
    ap.add_argument("--score-only", action="store_true", help="ask nothing, score the cache")
    ap.add_argument("--list", action="store_true", help="list the readings worth a look")
    args = ap.parse_args(argv)
    models = args.model or list(DEFAULT_MODELS)
    cards = read_cards()[: args.n or None]
    words = sum(len(card.furigana.review) for card in cards)
    print(f"{len(cards)} cards with {words} words in review", file=sys.stderr)
    cached = read_results()
    if not args.score_only:
        get_response = load_get_response(args.workers)
        for model in models:
            todo = [
                card
                for card in cards
                if capture_store.research_cache_key(model, card.prompt) not in cached
            ]
            print(f"{model}: {len(todo)} to ask, {len(cards) - len(todo)} cached", file=sys.stderr)
            if todo:
                fetch(get_response, model, todo, args.workers, cached)
    labels = fa.read_labels()
    for model in models:
        print("\n".join(evaluate([model], cards, cached, labels, args.list and len(models) == 1)))
    if len(models) > 1:
        print("\n".join(evaluate(models, cards, cached, labels, args.list)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
