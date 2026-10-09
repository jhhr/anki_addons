"""Run one transcriber over the furigana audio eval's picked clips and keep what it heard.

Each run writes `evals/furigana_audio/runs/<model>.jsonl` in the test data checkout, one row per
card: its id, the text the model gave and the seconds it took; `<model>.meta.json` beside it
says which revision of the model, with which settings, on what. Cards already in the file are
skipped, so a run that stops is started again and goes on where it was. While a run goes on,
its rows go to `<model>.jsonl.part`, which the test data repo ignores, and into the run file
when it ends: the tracked file changes once a run, not with every card, and a part a stopped
run leaves is picked up by the next.

A model hears the clip and nothing else: no subtitle, prompt or name list. Audio models follow
text they are given rather than the audio, and what each one hears by itself is what the eval
is about.

| model              | writes                                                               |
| ------------------ | -------------------------------------------------------------------- |
| kana-anime-whisper | rose3/kana-anime-whisper: anime-whisper tuned to write the readings  |
|                    | in hiragana, no kanji                                                |
| kana-whisper       | sbintuitions/kana-whisper: Whisper turbo tuned to write the          |
|                    | pronunciation in katakana (キョーワ)                                 |
| phone-accent       | AkitoP/whisper-large-v3-japense-phone_accent: katakana with pitch    |
|                    | accent marks                                                         |
| ruby-mora          | hshispeech/Ruby-ASR-1.7B, verbatim: the morae of its CTC head, from  |
|                    | the audio encoder alone                                              |
| ruby               | the same model's decoder: the text with readings, 漢字[かんじ]       |
| anime-whisper      | litagin/anime-whisper: ordinary text, for names and lines whose      |
|                    | caption is not what is said                                          |

The models need PyTorch and transformers, which the add-on does not use, so they go into an
environment of their own; Ruby-ASR's qwen-asr pins transformers 4.57.6:

    pip install torch transformers==4.57.6 qwen-asr==0.0.6 librosa soundfile \\
        "git+https://github.com/hshi-speech/Ruby-ASR-1.7B.git"
    python word_array/research/furigana_audio_run.py MODEL [--limit N] [--stratum S]
        [--device cuda]

`--stratum` takes the cards of one stratum only (`inline` is the 40 whose hard word the line
reads itself): Ruby-ASR's decoder takes over a minute a card on a 4-core CPU.

ffmpeg must be on PATH: the clips are Opus, and it decodes them for every model alike.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional, Sequence

import furigana_audio as fa

RUNS = fa.HOME / "runs"
SAMPLE_RATE = 16000
RUBY_REPO = "hshispeech/Ruby-ASR-1.7B"
# Of the repo's two variants, the one its paper finds better at readings
RUBY_VARIANT = "verbatim"


class Model(NamedTuple):
    repo: str
    kind: str  # "whisper", "ruby-mora" or "ruby"
    generate: dict  # Whisper generate() arguments besides the language
    # The repo whose generation config to use, for a fine-tune that ships none: without one
    # generate() cannot turn language="ja" into its token
    generation_config: Optional[str] = None
    # What its answer is: "kana", "ruby" (漢字[かんじ]) or "text", with kanji as usually written
    writes: str = "kana"


MODELS = {
    # The settings its model card gives
    "kana-anime-whisper": Model(
        "rose3/kana-anime-whisper", "whisper", {"num_beams": 2, "repetition_penalty": 1.1}
    ),
    "kana-whisper": Model("sbintuitions/kana-whisper", "whisper", {}),
    "phone-accent": Model(
        "AkitoP/whisper-large-v3-japense-phone_accent",
        "whisper",
        {},
        generation_config="openai/whisper-large-v3-turbo",
    ),
    "ruby-mora": Model(RUBY_REPO, "ruby-mora", {}),
    "ruby": Model(RUBY_REPO, "ruby", {}, writes="ruby"),
    # anime-whisper's own evaluation decoded with this against Whisper's repetition loops
    "anime-whisper": Model(
        "litagin/anime-whisper", "whisper", {"no_repeat_ngram_size": 5}, writes="text"
    ),
}

# Audio in, text out
Transcriber = Callable[[Any], str]


def module(name: str) -> Any:
    """A package of the models' environment, which the add-on's (and its type check) lacks."""
    return importlib.import_module(name)


def load_clip(path: Path) -> Any:
    """The clip as 16 kHz mono float32 samples."""
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1"]
        + ["-ar", str(SAMPLE_RATE), "-"],
        check=True,
        capture_output=True,
    ).stdout
    np = module("numpy")
    return np.frombuffer(raw, dtype=np.float32)


def whisper(model: Model, device: str) -> Transcriber:
    torch, transformers = module("torch"), module("transformers")
    processor = transformers.WhisperProcessor.from_pretrained(model.repo)
    net = transformers.WhisperForConditionalGeneration.from_pretrained(model.repo)
    if model.generation_config:
        net.generation_config = transformers.GenerationConfig.from_pretrained(
            model.generation_config
        )
    net = net.to(device).eval()

    def transcribe(wav: Any) -> str:
        features = processor(wav, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        with torch.inference_mode():
            ids = net.generate(
                features.input_features.to(device),
                language="ja",
                task="transcribe",
                **model.generate,
            )
        return str(processor.batch_decode(ids, skip_special_tokens=True)[0])

    return transcribe


def ruby_mora(model: Model, device: str) -> Transcriber:
    recognizer = module("ruby_asr").MoraCTCRecognizer.from_pretrained(
        model.repo, device=device, subfolder=RUBY_VARIANT
    )
    return lambda wav: str(recognizer.transcribe(wav)[0])


def ruby(model: Model, device: str) -> Transcriber:
    torch, hub = module("torch"), module("huggingface_hub")
    local = Path(hub.snapshot_download(model.repo, allow_patterns=[f"{RUBY_VARIANT}/*"]))
    asr = module("qwen_asr").Qwen3ASRModel.from_pretrained(
        str(local / RUBY_VARIANT),
        dtype=torch.float32 if device == "cpu" else torch.bfloat16,
        device_map=device,
    )
    # No language and no context: the readings are the model's behaviour under the prompt it
    # was trained with, an empty one
    return lambda wav: str(asr.transcribe(audio=(wav, SAMPLE_RATE))[0].text)


LOADERS: dict[str, Callable[[Model, str], Transcriber]] = {
    "whisper": whisper,
    "ruby-mora": ruby_mora,
    "ruby": ruby,
}


def meta(name: str, model: Model, device: str) -> dict:
    hub = module("huggingface_hub")
    return {
        "model": name,
        "repo": model.repo,
        "revision": hub.model_info(model.repo).sha,
        "kind": model.kind,
        "generate": model.generate,
        "device": device,
        "torch": module("torch").__version__,
        "transformers": module("transformers").__version__,
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
        module("torch").set_num_threads(os.cpu_count() or 1)
    transcribe = LOADERS[model.kind](model, device)
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{name}.meta.json").write_text(
        json.dumps(meta(name, model, device), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with part.open("a", encoding="utf-8", newline="\n") as f:
        for n, row in enumerate(todo, 1):
            wav = load_clip(fa.AUDIO / row["audio"])
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
