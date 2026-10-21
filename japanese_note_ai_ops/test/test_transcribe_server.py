"""dev/transcribe_server.py: what /transcribe and /health answer, over real HTTP, with stand-ins
for the models and the decoder, so neither PyTorch nor PyAV is needed; and how the add-on's own
client (base_ops.get_transcript_from_local_server) takes those answers. The decoder itself is
tested where PyAV is installed, the server's environment, and skipped elsewhere."""

from __future__ import annotations

import base64
import contextlib
import http.client
import importlib.util
import io
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

from addon_modules import ADDON_ROOT, FakeClock, load_ops_module


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
api = load_ops_module("api_client")
base_ops = load_ops_module("base_ops")
run_errors = load_ops_module("run_errors")


class OutOfMemoryError(RuntimeError):
    """torch.cuda.OutOfMemoryError's stand-in: the server knows it by its name."""


def fails_to_decode(data: bytes):
    raise ts.ClipError("Invalid data found when processing input")


def fails_with(error: Exception):
    """A model that raises `error` on every clip."""

    def transcribe(wav):
        raise error

    return transcribe


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

    def test_a_clip_that_cannot_be_decoded(self):
        service = ts.Service({"ruby-mora": lambda wav: ""}, {}, decoder=fails_to_decode)
        status, answer = service.transcribe(b"junk")
        self.assertEqual(status, 400)
        self.assertIn("Invalid data found", answer["error"])

    def failing(self, error: Exception):
        """A service whose one model raises `error` on every clip."""
        return ts.Service({"ruby-mora": fails_with(error)}, {}, decoder=lambda data: data)

    def test_a_model_failing_on_a_clip_is_a_final_answer(self):
        with contextlib.redirect_stderr(io.StringIO()) as console:
            status, answer = self.failing(RuntimeError("no frames")).transcribe(b"clip")
        self.assertEqual(status, 422)
        self.assertEqual(answer["error"], "ruby-mora failed on the clip: RuntimeError: no frames")
        self.assertIn("ruby-mora failed on a clip", console.getvalue())
        self.assertIn("RuntimeError: no frames", console.getvalue(), "the traceback")

    def test_a_model_out_of_memory_may_be_asked_again(self):
        for error in (
            OutOfMemoryError("CUDA out of memory. Tried to allocate 20.00 MiB"),
            RuntimeError("CUDA error: out of memory"),
            MemoryError(),
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.failing(error).transcribe(b"clip")[0], 503, repr(error))

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
        broken = fails_with(RuntimeError("no frames"))
        transcribers = {"kana-anime-whisper": lambda wav: f"き{len(wav)}", "ruby-mora": broken}
        self.decoded: list[bytes] = []
        service = ts.Service(transcribers, {}, lambda data: self.decoded.append(data) or data)
        self.server = ts.Server(("127.0.0.1", 0), ts.make_handler(service))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, data=None, content_type: str | None = "application/json"):
        """(status, answer) of a GET, or of a POST of `data` sent as `content_type`, or with
        none."""
        host, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(str(host), port, timeout=10)
        try:
            headers = {"Content-Type": content_type} if data is not None and content_type else {}
            connection.request("GET" if data is None else "POST", path, data, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def clip(self, data: bytes, *models: str) -> bytes:
        audio = base64.b64encode(data).decode("ascii")
        return json.dumps({"audio": audio, "models": list(models)}).encode("utf-8")

    def test_a_clip_posted_and_its_transcript(self):
        status, answer = self.request("/transcribe", self.clip(b"12345", "kana-anime-whisper"))
        self.assertEqual((status, answer["transcripts"]), (200, {"kana-anime-whisper": "き5"}))

    def test_a_body_that_is_not_json_with_base64_audio_is_refused(self):
        for body in (b"12345", b'{"audio": "not base64!"}', b"[1]"):
            status, answer = self.request("/transcribe", body)
            self.assertEqual(status, 400, body)
            self.assertIn("base64", answer["error"])

    def test_a_post_not_sent_as_json_is_refused(self):
        """A web page may send another site a form, text/plain or an untyped body without a
        CORS preflight; JSON only after one, which the server never answers."""
        body = self.clip(b"12345", "kana-anime-whisper")
        for content_type in (
            "text/plain",
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=x",
            None,
        ):
            self.assertEqual(self.request("/transcribe", body, content_type)[0], 415, content_type)
        self.assertEqual(self.decoded, [])
        status, _ = self.request("/transcribe", body, "application/json; charset=utf-8")
        self.assertEqual((status, self.decoded), (200, [b"12345"]))

    def test_a_model_failing_answers_with_its_error_and_the_server_goes_on(self):
        with contextlib.redirect_stderr(io.StringIO()):
            status, answer = self.request("/transcribe", self.clip(b"12345", "ruby-mora"))
        self.assertEqual(status, 422)
        self.assertIn("no frames", answer["error"])
        self.assertEqual(self.request("/health")[0], 200)

    def test_health_and_unknown_paths(self):
        status, answer = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(answer["models"]), ["kana-anime-whisper", "ruby-mora"])
        self.assertEqual(self.request("/nothing")[0], 404)
        self.assertEqual(self.request("/nothing", b"x")[0], 404)


class ClientTest(unittest.TestCase):
    """The add-on's client against the server: what it sends, and which answers it sends a
    clip again for."""

    def setUp(self):
        # The client's sessions and cooldowns are the module's; this test's are its own
        sessions = dict(api._sessions)
        api._sessions.clear()
        self.addCleanup(self.restore_sessions, sessions)
        api.rate_limit_tracker.reset()
        self.addCleanup(api.rate_limit_tracker.reset)
        patcher = mock.patch.object(api, "time", FakeClock())  # no backoff waited out
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reports: list[str] = []
        deliver = run_errors.deliver_with(lambda _title, text: self.reports.append(text))
        self.addCleanup(run_errors.deliver_with, deliver)
        self.asked = 0
        self.content_types: list[str] = []

    @staticmethod
    def restore_sessions(sessions) -> None:
        for session in api._sessions.values():
            session.close()
        api._sessions.clear()
        api._sessions.update(sessions)

    def serve(self, *outcomes) -> dict:
        """A server whose ruby-mora gives each outcome in turn, an answer or an error to raise;
        the client's config for it."""

        def transcribe(wav):
            outcome = outcomes[self.asked]
            self.asked += 1
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        service = ts.Service({"ruby-mora": transcribe}, {}, decoder=lambda data: data)
        content_types = self.content_types

        class Recording(ts.make_handler(service)):  # type: ignore[misc]
            def do_POST(self):
                content_types.append(self.headers.get("Content-Type"))
                super().do_POST()

        server = ts.Server(("127.0.0.1", 0), Recording)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        return {"transcribe_url": url, "max_request_retries": 5, "request_timeout": 10}

    def transcribe(self, config: dict):
        with contextlib.redirect_stderr(io.StringIO()):
            return base_ops.get_transcript_from_local_server("local-ruby-mora", b"OggS", config)

    def test_the_client_sends_its_clip_as_json(self):
        config = self.serve("ネコ")
        self.assertEqual(self.transcribe(config), {"text": "ネコ"})
        self.assertEqual(self.content_types, ["application/json"])

    def test_a_clip_a_model_fails_on_is_sent_once_and_the_error_reported(self):
        config = self.serve(*[RuntimeError("no frames")] * 6)
        self.assertIsNone(self.transcribe(config))
        self.assertEqual(self.asked, 1)
        self.assertEqual(len(self.reports), 1)
        self.assertIn("local-ruby-mora: HTTP 422", self.reports[0])
        self.assertIn("ruby-mora failed on the clip: RuntimeError: no frames", self.reports[0])

    def test_a_model_out_of_memory_is_asked_again(self):
        config = self.serve(OutOfMemoryError("CUDA out of memory"), "ネコ")
        self.assertEqual(self.transcribe(config), {"text": "ネコ"})
        self.assertEqual((self.asked, self.reports), (2, []))


def importable(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


@unittest.skipUnless(importable("av") and importable("numpy"), "PyAV: the server's environment")
class DecodeTest(unittest.TestCase):
    """The server's decoder on clips in the formats subs2srs writes, Ogg Opus, MP3 and an M4A
    with its index at the end, each 1.5 s of a tone at 48 kHz in each channel of two."""

    av: Any
    np: Any
    clips: dict[str, bytes]

    @classmethod
    def setUpClass(cls):
        # By name, as the server does: the add-on's environment (and its type check) has neither
        cls.av, cls.np = ts.module("av"), ts.module("numpy")
        formats = {"opus": ("ogg", "libopus"), "mp3": ("mp3", "libmp3lame"), "m4a": ("ipod", "aac")}
        cls.clips = {name: cls.encode(*format) for name, format in formats.items()}

    @classmethod
    def encode(cls, container_format: str, codec: str) -> bytes:
        np, rate = cls.np, 48000
        t = np.arange(int(1.5 * rate)) / rate
        tones = np.stack([0.3 * np.sin(2 * np.pi * 440 * t), 0.2 * np.sin(2 * np.pi * 660 * t)])
        frame = cls.av.AudioFrame.from_ndarray(tones.astype(np.float32), "fltp", "stereo")
        frame.sample_rate = rate
        out = io.BytesIO()
        with cls.av.open(out, "w", format=container_format) as container:
            stream = container.add_stream(codec, rate=rate, layout="stereo")
            for packet in [*stream.encode(frame), *stream.encode(None)]:
                container.mux(packet)
        return out.getvalue()

    def test_each_clip_as_16khz_mono_float32(self):
        m4a = self.clips["m4a"]
        self.assertLess(m4a.find(b"mdat"), m4a.find(b"moov"), "an M4A a pipe could not decode")
        np = self.np
        for name, clip in self.clips.items():
            wav = ts.decode(clip)
            self.assertEqual((wav.dtype, wav.ndim), (np.float32, 1), name)
            # 1.5 s, give or take what the encoder pads
            self.assertAlmostEqual(len(wav) / ts.SAMPLE_RATE, 1.5, delta=0.05, msg=name)
            # Each channel mixed in at -3 dB, as ffmpeg's -ac 1 does it, measured over the middle
            # second: whole periods of both tones
            middle = wav[4000:20000]
            t = np.arange(len(middle)) / ts.SAMPLE_RATE
            for hz, amplitude in ((440, 0.3), (660, 0.2)):
                heard = 2 * abs(np.mean(middle * np.exp(-2j * np.pi * hz * t)))
                expected = amplitude / np.sqrt(2)
                self.assertAlmostEqual(heard, expected, delta=expected / 10, msg=f"{name} {hz}")

    @unittest.skipUnless(shutil.which("ffmpeg"), "no ffmpeg on PATH to compare with")
    def test_the_samples_of_the_ffmpeg_command_it_replaced(self):
        np = self.np
        for name, clip in self.clips.items():
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / f"clip.{name}"
                path.write_bytes(clip)
                command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le"]
                command += ["-ac", "1", "-ar", str(ts.SAMPLE_RATE), "-"]
                raw = subprocess.run(command, check=True, capture_output=True).stdout
            before, now = np.frombuffer(raw, dtype=np.float32), ts.decode(clip)
            self.assertLessEqual(abs(len(now) - len(before)), 4, name)
            n = min(len(now), len(before))
            self.assertLess(float(np.max(np.abs(now[:n] - before[:n]))), 1e-3, name)

    def test_a_clip_file_as_the_eval_reads_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "clip.opus"
            path.write_bytes(self.clips["opus"])
            from_file = ts.load_clip(path)
        self.assertTrue(self.np.array_equal(from_file, ts.decode(self.clips["opus"])))

    def test_what_is_no_clip_is_refused(self):
        subtitles = b"1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"
        for data in (b"no audio at all", subtitles):
            with self.assertRaises(ts.ClipError, msg=data):
                ts.decode(data)
        service = ts.Service({"ruby-mora": lambda wav: "ネコ"}, {})
        status, answer = service.transcribe(subtitles)
        self.assertEqual(status, 400)
        self.assertEqual(answer["error"], "the clip could not be decoded: no audio in the clip")


class PortTest(unittest.TestCase):
    def test_a_second_server_cannot_take_the_port(self):
        """On Windows a socket may bind a port another holds when both ask to reuse the
        address, and requests then reach either: the server does not ask, so a second one
        started on its port fails to start."""
        handler = ts.make_handler(ts.Service({}, {}))
        first = ts.Server(("127.0.0.1", 0), handler)
        self.addCleanup(first.server_close)
        self.assertEqual(first.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
        with self.assertRaises(OSError):
            ts.Server(("127.0.0.1", first.server_address[1]), handler).server_close()


if __name__ == "__main__":
    unittest.main()
