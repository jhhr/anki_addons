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
`claude -p` as the op does. Only `terminal-` models: the add-on's config is read with
meta.json's settings over it, but never an API key (AGENTS.md). Answers are cached by model and
prompt in `evals/furigana_audio/arbiter_results.jsonl` of the test data checkout, each as it
comes back: a rerun pays only for prompts that changed, and Ctrl+C keeps every answer already
back and stops the `claude` processes still running. `--workers` is requests at once, and for
`terminal-` models the cap on live `claude` processes.

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
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
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
# The config keys that hold a secret, which are dropped from meta.json's settings
SECRET_RE = re.compile(r"api_?key|token|secret|password", re.I)


class Card:
    def __init__(self, row: dict, furigana, inputs: dict) -> None:
        self.row = row
        self.furigana = furigana
        self.inputs = inputs
        # A card none of whose words in review was heard is asked nothing, as the op asks
        # nothing for its note (furigana_arbiter.asked)
        self.prompt = arbiter.review_prompt(**inputs) if inputs["words"] else ""


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


def without_secrets(config: dict) -> dict:
    """`config` without the keys that hold a secret (an API key), at any depth."""
    return {
        key: without_secrets(value) if isinstance(value, dict) else value
        for key, value in config.items()
        if not SECRET_RE.search(key)
    }


def load_get_response(workers: int, models: Sequence[str]):
    """The op's `get_response`, loaded with Anki stubbed and the add-on's config behind `mw`:
    config.json with meta.json's settings over it, without a secret. So only `terminal-` models
    can be asked, which `claude -p` answers with no key."""
    keyed = [model for model in models if not model.startswith("terminal-")]
    if keyed:
        raise SystemExit(f"only terminal- models are asked here, which need no API key: {keyed}")
    sys.path.insert(0, str(ADDON_ROOT / "test"))
    from addon_modules import load_ops_module, mw

    config = without_secrets(json.loads((ADDON_ROOT / "config.json").read_text(encoding="utf-8")))
    meta = ADDON_ROOT / "meta.json"
    if meta.exists():
        settings = json.loads(meta.read_text(encoding="utf-8")).get("config", {})
        config.update(without_secrets(settings))
    # Here --workers is the cap on live `claude` processes, over every model of the run
    config["terminal_max_concurrent_requests"] = workers
    setattr(mw, "addonManager", SimpleNamespace(getConfig=lambda _name: config))
    return load_ops_module("base_ops").get_response


def read_results() -> dict[str, dict]:
    return {row["key"]: row for row in fa.read_jsonl(RESULTS)}


class Cancel:
    """A cancel state for the op's request code (base_ops.CancelState's shape): once set, a
    request still running stops, its `claude` process killed and not sent again."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def is_cancelled(self) -> bool:
        return self.event.is_set()


def fetch(get_response, model: str, todo: list[Card], workers: int, cached: dict) -> None:
    """Asks `model` about each card of `todo`, `workers` at a time, and caches each answer as
    it comes back. On Ctrl+C the requests not sent yet are dropped and those running stopped,
    and every answer already back is kept: an answer behind a slow one was lost before."""
    cancel = Cancel()

    def ask(card: Card):
        return get_response(
            model,
            card.prompt,
            cancel_state=cancel,
            response_schema=arbiter.RESPONSE_SCHEMA,
            kind=KIND,
            inputs=card.inputs,
        )

    started = time.monotonic()
    counts: Counter = Counter()
    pool = ThreadPoolExecutor(workers)
    futures: dict[Future, Card] = {pool.submit(ask, card): card for card in todo}
    with RESULTS.open("a", encoding="utf-8") as out:

        def save(future: Future) -> None:
            card = futures.pop(future)
            response = None if future.cancelled() else future.result()
            counts["done"] += 1
            if not isinstance(response, dict):
                counts["failed"] += 1
                return  # not cached, so a rerun asks again
            key = capture_store.research_cache_key(model, card.prompt)
            cached[key] = {"key": key, "model": model, "id": card.row["id"], "response": response}
            out.write(json.dumps(cached[key], ensure_ascii=False) + "\n")
            out.flush()
            if counts["done"] % 25 == 0:
                print(f"  {model} ...{counts['done']}", file=sys.stderr)

        try:
            for future in as_completed(list(futures)):
                save(future)
        except KeyboardInterrupt:
            cancel.event.set()
            pool.shutdown(wait=True, cancel_futures=True)
            for future in [future for future in futures if future.done()]:
                save(future)
            kept = counts["done"] - counts["failed"]
            print(f"{model}: stopped, {kept} answers kept", file=sys.stderr)
            raise
        finally:
            pool.shutdown(wait=False)
    minutes = (time.monotonic() - started) / 60
    print(
        f"{model}: {len(todo)} requests ({counts['failed']} failed) in {minutes:.1f} min",
        file=sys.stderr,
    )


def evaluate(
    models: Sequence[str], cards: list[Card], cached: dict, labels: dict, listing: bool
) -> list[str]:
    """The op's arbiter with `models`, all of them agreeing, over the cards."""
    c: Counter = Counter()
    listed: list[str] = []
    for card in cards:
        review = card.furigana.review
        # The words asked about, numbered as the op numbers them; the rest are never written
        asked = arbiter.asked(card.furigana)
        number = {id(r): n for n, r in enumerate(asked, 1)}
        answers = []
        unanswered = False
        for model in models:
            key = capture_store.research_cache_key(model, card.prompt)
            row = cached.get(key) if card.prompt else None
            unanswered |= bool(card.prompt) and row is None
            answers.append(
                arbiter.answers_from_response(row["response"], len(asked)) if row else {}
            )
        # A card some model has not answered yet, once whatever the number of such models
        c["unanswered cards"] += unanswered
        left = False
        for r in review:
            label = fa.label_for(labels, (card.row["id"], r.line, r.start), r.surface)
            said = label["reading"] if label and label["verdict"] == "heard" else None
            kind = "mismatch" if r.why == "mismatch" else "unsettled"
            c[f"{kind}: words"] += 1
            n = number.get(id(r))
            given_answers = [by_model.get(n) for by_model in answers] if n else []
            written = arbiter.agreed_reading(r, given_answers) if n else None
            if written is None:
                left = True
                if listing and said is not None:
                    given = ", ".join(
                        f"{a.reading}{'' if a.sure else '?'}" if a else "-"
                        for a in given_answers
                    ) or "(not asked)"
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
        get_response = load_get_response(args.workers, models)
        asking = [card for card in cards if card.prompt]
        for model in models:
            todo = [
                card
                for card in asking
                if capture_store.research_cache_key(model, card.prompt) not in cached
            ]
            print(
                f"{model}: {len(todo)} to ask, {len(asking) - len(todo)} cached", file=sys.stderr
            )
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
