"""dev/transcribe_server.py: what /transcribe and /health answer, over real HTTP, with stand-ins
for the models and the decoder, so neither PyTorch nor ffmpeg is needed."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import ModuleType

from addon_modules import ADDON_ROOT


def load_server() -> ModuleType:
    """dev/transcribe_server.py, loaded by path as the research runner loads it."""
    spec = importlib.util.spec_from_file_location(
        "jnaio_transcribe_server_under_test", ADDON_ROOT / "dev" / "transcribe_server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ts = load_server()


def fails_to_decode(data: bytes):
    raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"Invalid data found")


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.heard: list = []

        def kana(wav):
            self.heard.append(("kana", wav))
            return "ねこ"

        transcribers = {"kana-anime-whisper": kana, "ruby-mora": lambda wav: "ネコ"}
        devices = {"kana-anime-whisper": "cuda"}
        self.service = ts.Service(transcribers, devices, decoder=lambda data: data.decode())

    def test_every_loaded_model_hears_the_clip_by_default(self):
        status, answer = self.service.transcribe(b"clip")
        self.assertEqual(status, 200)
        self.assertEqual(answer["transcripts"], {"kana-anime-whisper": "ねこ", "ruby-mora": "ネコ"})
        self.assertEqual(set(answer["seconds"]), {"kana-anime-whisper", "ruby-mora"})
        self.assertEqual(self.heard, [("kana", "clip")])

    def test_the_models_asked_for(self):
        status, answer = self.service.transcribe(b"clip", ["ruby-mora"])
        self.assertEqual((status, answer["transcripts"]), (200, {"ruby-mora": "ネコ"}))
        self.assertEqual(self.heard, [])

    def test_a_model_not_loaded_is_refused(self):
        status, answer = self.service.transcribe(b"clip", ["kana-whisper"])
        self.assertEqual(status, 400)
        self.assertIn("kana-whisper", answer["error"])

    def test_no_audio_is_refused(self):
        self.assertEqual(self.service.transcribe(b"")[0], 400)

    def test_a_clip_ffmpeg_cannot_read(self):
        service = ts.Service({"ruby-mora": lambda wav: ""}, {}, decoder=fails_to_decode)
        status, answer = service.transcribe(b"junk")
        self.assertEqual(status, 400)
        self.assertIn("Invalid data found", answer["error"])

    def test_health_names_each_models_revision_and_device(self):
        models = self.service.health()["models"]
        self.assertEqual(
            models["kana-anime-whisper"],
            {
                "repo": "rose3/kana-anime-whisper",
                "revision": ts.MODELS["kana-anime-whisper"].revision,
                "device": "cuda",
            },
        )
        self.assertEqual(models["ruby-mora"]["device"], "cpu")


class HttpTest(unittest.TestCase):
    def setUp(self):
        def broken(wav):
            raise RuntimeError("out of memory")

        transcribers = {"kana-anime-whisper": lambda wav: f"き{len(wav)}", "ruby-mora": broken}
        service = ts.Service(transcribers, {}, decoder=lambda data: data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ts.make_handler(service))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path, data=None):
        req = urllib.request.Request(self.url + path, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_clip_posted_and_its_transcript(self):
        status, answer = self.request("/transcribe?models=kana-anime-whisper", b"12345")
        self.assertEqual((status, answer["transcripts"]), (200, {"kana-anime-whisper": "き5"}))

    def test_a_model_failing_answers_with_its_error_and_the_server_goes_on(self):
        status, answer = self.request("/transcribe?models=ruby-mora", b"12345")
        self.assertEqual(status, 500)
        self.assertIn("out of memory", answer["error"])
        self.assertEqual(self.request("/health")[0], 200)

    def test_health_and_unknown_paths(self):
        status, answer = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(answer["models"]), ["kana-anime-whisper", "ruby-mora"])
        self.assertEqual(self.request("/nothing")[0], 404)
        self.assertEqual(self.request("/nothing", b"x")[0], 404)


if __name__ == "__main__":
    unittest.main()
