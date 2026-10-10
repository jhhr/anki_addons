"""A transcription server for "Transcribe sentence audio": the kana transcribers the furigana
audio eval picked, run on this machine and asked over HTTP, so Anki's own Python never loads
PyTorch.

A model hears the clip and nothing else: no subtitle, prompt or name list. Audio models follow
text they are given rather than the audio, and what each hears by itself is what the op's check
(`word_array/reading_check.py`) compares with the draft.

| model              | writes                                                               |
| ------------------ | -------------------------------------------------------------------- |
| kana-anime-whisper | rose3/kana-anime-whisper: anime-whisper tuned to write the readings  |
|                    | in hiragana, no kanji                                                |
| ruby-mora          | hshispeech/Ruby-ASR-1.7B, verbatim: the morae of its CTC head, from  |
|                    | the audio encoder alone                                              |
| kana-whisper       | sbintuitions/kana-whisper: Whisper turbo tuned to write the          |
|                    | pronunciation in katakana (キョーワ)                                 |
| phone-accent       | AkitoP/whisper-large-v3-japense-phone_accent: katakana with pitch    |
|                    | accent marks                                                         |
| ruby               | the same model's decoder: the text with readings, 漢字[かんじ]       |
| anime-whisper      | litagin/anime-whisper: ordinary text, for names and lines whose      |
|                    | caption is not what is said                                          |

The op's are the first two: on the eval's 400 clips, checked by ear, that pair wrote one wrong
reading among the 846 it wrote with a known answer. The others are here for the eval
(`word_array/research/furigana_audio_run.py` runs every model through this module). Each is
pinned to the revision the eval measured (`Model.revision`): rose3 retrains kana-anime-whisper
now and then.

Setup, once, in an environment of its own (the add-on's Python has none of these packages):

    python -m venv transcribe-env
    transcribe-env\\Scripts\\activate          (macOS and Linux: source transcribe-env/bin/activate)
    pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cu126
    pip install -r dev/transcribe_server_requirements.txt

The CUDA 12.6 wheels are the newest that still run on a Maxwell card such as the GTX 970;
without an NVIDIA card take https://download.pytorch.org/whl/cpu instead. ffmpeg must be on
PATH: it decodes the clips. Then, from the add-on's folder, leave this running while Anki
transcribes:

    python dev/transcribe_server.py [--models kana-anime-whisper ruby-mora] [--device cuda]
        [--port 8792]

The first start downloads the models into the Hugging Face cache, about 5.5 GB for the two.
`--device cuda` puts the Whisper models on the GPU in float16, about 1.5 GB of it for
kana-anime-whisper; ruby-mora stays on the CPU, where it took 0.4 s a clip in the eval, unless
`--ruby-device` says otherwise. Only its audio encoder and mora head run, so the rest of that
2B-parameter model is dropped once loaded.

    GET  /health      the models loaded: repo, revision, device
    POST /transcribe  the clip's bytes, in any format ffmpeg reads; `?models=a,b` picks some of
                      the loaded ones -> {"transcripts": {model: kana}, "seconds": {model: s}}

It listens on 127.0.0.1 unless `--host` says otherwise, and asks for no password: whatever can
reach its port can have clips transcribed.
"""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional, Sequence
from urllib.parse import parse_qs, urlparse

SAMPLE_RATE = 16000
RUBY_REPO = "hshispeech/Ruby-ASR-1.7B"
RUBY_REVISION = "22839f9416eeb4bf993e3f315d84cac33ab27a6f"
# Of the repo's two variants, the one its paper finds better at readings
RUBY_VARIANT = "verbatim"
PORT = 8792
# The models the op uses, the ones started by default
OP_MODELS = ("kana-anime-whisper", "ruby-mora")


class Model(NamedTuple):
    repo: str
    kind: str  # "whisper", "ruby-mora" or "ruby"
    generate: dict  # Whisper generate() arguments besides the language
    # The commit of the repo the eval measured
    revision: Optional[str] = None
    # The repo whose generation config to use, for a fine-tune that ships none: without one
    # generate() cannot turn language="ja" into its token
    generation_config: Optional[str] = None
    # What its answer is: "kana", "ruby" (漢字[かんじ]) or "text", with kanji as usually written
    writes: str = "kana"


MODELS = {
    # The settings its model card gives
    "kana-anime-whisper": Model(
        "rose3/kana-anime-whisper",
        "whisper",
        {"num_beams": 2, "repetition_penalty": 1.1},
        revision="7139c040a7c3d4dcb579c3e771e69fa0f37079fa",
    ),
    "kana-whisper": Model(
        "sbintuitions/kana-whisper",
        "whisper",
        {},
        revision="88ecb3d79c5846cb4fcf76f4107b84c8fa2acd82",
    ),
    "phone-accent": Model(
        "AkitoP/whisper-large-v3-japense-phone_accent",
        "whisper",
        {},
        revision="743b64d309a5a5127282ff77a926211f2652ebb1",
        generation_config="openai/whisper-large-v3-turbo",
    ),
    "ruby-mora": Model(RUBY_REPO, "ruby-mora", {}, revision=RUBY_REVISION),
    "ruby": Model(RUBY_REPO, "ruby", {}, revision=RUBY_REVISION, writes="ruby"),
    # anime-whisper's own evaluation decoded with this against Whisper's repetition loops
    "anime-whisper": Model(
        "litagin/anime-whisper",
        "whisper",
        {"no_repeat_ngram_size": 5},
        revision="22e2008a8182b357da3922a6308d095008f72973",
        writes="text",
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


def decode(data: bytes) -> Any:
    """A clip sent as bytes, as `load_clip` reads one from a file. Through a file rather than a
    pipe: an MP4 or M4A clip keeps its index at the end, which ffmpeg cannot reach in a pipe."""
    handle, name = tempfile.mkstemp(suffix=".clip")
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(data)
        return load_clip(Path(name))
    finally:
        os.unlink(name)


def snapshot(model: Model, *patterns: str) -> Path:
    """The model's files at its revision, downloaded once into the Hugging Face cache."""
    hub = module("huggingface_hub")
    allow = list(patterns) or None
    return Path(hub.snapshot_download(model.repo, revision=model.revision, allow_patterns=allow))


def whisper(model: Model, device: str) -> Transcriber:
    torch, transformers = module("torch"), module("transformers")
    dtype = torch.float32 if device == "cpu" else torch.float16
    processor = transformers.WhisperProcessor.from_pretrained(
        model.repo, revision=model.revision
    )
    net = transformers.WhisperForConditionalGeneration.from_pretrained(
        model.repo, revision=model.revision, dtype=dtype
    )
    if model.generation_config:
        net.generation_config = transformers.GenerationConfig.from_pretrained(
            model.generation_config
        )
    net = net.to(device).eval()

    def transcribe(wav: Any) -> str:
        features = processor(wav, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        with torch.inference_mode():
            ids = net.generate(
                features.input_features.to(device, dtype),
                language="ja",
                task="transcribe",
                **model.generate,
            )
        return str(processor.batch_decode(ids, skip_special_tokens=True)[0])

    return transcribe


def ruby_mora(model: Model, device: str) -> Transcriber:
    # Loaded on the CPU in float32 whatever the device: the package moves the whole model to
    # its device, which a 4 GB card cannot hold
    root = snapshot(model, f"{RUBY_VARIANT}/*")
    recognizer = module("ruby_asr").MoraCTCRecognizer.from_pretrained(
        str(root), device="cpu", subfolder=RUBY_VARIANT
    )
    # This mode runs the audio encoder and the CTC branch only; the text decoder is most of
    # the 2B parameters
    thinker = recognizer.model.thinker
    del thinker.model, thinker.lm_head
    gc.collect()
    if device != "cpu":
        recognizer.model.to(device)
        recognizer.ctc_branch.to(device)
    return lambda wav: str(recognizer.transcribe(wav)[0])


def ruby(model: Model, device: str) -> Transcriber:
    torch = module("torch")
    local = snapshot(model, f"{RUBY_VARIANT}/*")
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


def load(name: str, device: str) -> Transcriber:
    model = MODELS[name]
    return LOADERS[model.kind](model, device)


class Service:
    """The loaded transcribers and what /transcribe and /health answer, apart from HTTP so a
    test can drive it with stand-ins. One clip is transcribed at a time: the models share the
    CPU's cores or the GPU's memory, and Anki asks with many threads at once."""

    def __init__(
        self,
        transcribers: dict[str, Transcriber],
        devices: dict[str, str],
        decoder: Callable[[bytes], Any] = decode,
    ):
        self.transcribers = transcribers
        self.devices = devices
        self.decoder = decoder
        self.lock = threading.Lock()

    def health(self) -> dict:
        return {
            "models": {
                name: {
                    "repo": MODELS[name].repo,
                    "revision": MODELS[name].revision,
                    "device": self.devices.get(name, "cpu"),
                }
                for name in self.transcribers
            }
        }

    def transcribe(self, data: bytes, wanted: Sequence[str] = ()) -> tuple[int, dict]:
        """(HTTP status, answer) for a clip and the models asked for, all loaded ones by
        default."""
        names = list(wanted) or list(self.transcribers)
        unknown = [n for n in names if n not in self.transcribers]
        if unknown:
            loaded = ", ".join(self.transcribers)
            return 400, {"error": f"not loaded here: {', '.join(unknown)} (loaded: {loaded})"}
        if not data:
            return 400, {"error": "no audio in the request"}
        try:
            wav = self.decoder(data)
        except subprocess.CalledProcessError as e:
            detail = e.stderr.decode("utf-8", "replace").strip() if e.stderr else str(e)
            return 400, {"error": f"ffmpeg could not decode the clip: {detail}"}
        transcripts: dict[str, str] = {}
        seconds: dict[str, float] = {}
        with self.lock:
            for name in names:
                start = time.perf_counter()
                transcripts[name] = self.transcribers[name](wav)
                seconds[name] = round(time.perf_counter() - start, 2)
        return 200, {"transcripts": transcripts, "seconds": seconds}


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path == "/health":
                self._send(200, service.health())
            else:
                self._send(404, {"error": f"no {self.path} here"})

        def do_POST(self):
            url = urlparse(self.path)
            if url.path != "/transcribe":
                self._send(404, {"error": f"no {url.path} here"})
                return
            wanted = [
                name
                for value in parse_qs(url.query).get("models", [])
                for name in value.split(",")
                if name
            ]
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length > 0 else b""
            try:
                status, payload = service.transcribe(data, wanted)
            except Exception as e:  # a model failing on one clip must not take the server down
                status, payload = 500, {"error": f"{type(e).__name__}: {e}"}
            self._send(status, payload)

    return Handler


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", nargs="+", default=list(OP_MODELS), choices=sorted(MODELS))
    ap.add_argument("--device", default="cpu", help='the Whisper models\': "cpu" or "cuda"')
    ap.add_argument("--ruby-device", default="cpu", help="the Ruby-ASR models'")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    torch = module("torch")
    torch.set_num_threads(os.cpu_count() or 1)
    transcribers: dict[str, Transcriber] = {}
    devices: dict[str, str] = {}
    for name in args.models:
        device = args.device if MODELS[name].kind == "whisper" else args.ruby_device
        print(f"loading {name} on {device}...", flush=True)
        transcribers[name] = load(name, device)
        devices[name] = device
    handler = make_handler(Service(transcribers, devices))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}"
    print(f"transcribing on {url} with {', '.join(args.models)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
