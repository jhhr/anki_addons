"""Transcribe sentence audio (async_api_ops/transcribe_audio.py): the transcription field's
lines, which transcribers a note is asked for, what goes with each request, and the check that
the local server is up before a run with a clip to send. The requests themselves, and their
capture, are in test_capture_calls.py; the server in test_transcribe_server.py."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import requests

from addon_modules import load_ops_module

op = load_ops_module("transcribe_audio")
run_errors = load_ops_module("run_errors")

SENTENCE = "Sentence note"
CONFIG = {
    SENTENCE: {
        "word_list_field": "Words",
        "sentence_audio_field": "Audio",
        "sentence_transcription_field": "Heard",
    },
    "transcribe_models": ["local-kana-anime-whisper", "local-ruby-mora"],
}
CLIP = b"OggS clip bytes"
CLIP_TAG = "[sound:clip.opus]"


class Note:
    def __init__(self, fields: dict[str, str], note_id: int = 7) -> None:
        self.id = note_id
        self.fields = dict(fields)

    def note_type(self):
        return {"name": SENTENCE}

    def __contains__(self, field: str) -> bool:
        return field in self.fields

    def __getitem__(self, field: str) -> str:
        return self.fields[field]

    def __setitem__(self, field: str, value: str) -> None:
        self.fields[field] = value


class FieldTest(unittest.TestCase):
    def test_a_line_per_transcriber_in_the_configs_order(self):
        text = op.format_transcripts(
            {"ruby-mora": "ネコ", "kana-anime-whisper": "ねこ", "old-model": "ねこ"},
            CONFIG["transcribe_models"],
        )
        self.assertEqual(text, "kana-anime-whisper: ねこ<br>ruby-mora: ネコ<br>old-model: ねこ")
        self.assertEqual(
            op.parse_transcripts(text),
            {"kana-anime-whisper": "ねこ", "ruby-mora": "ネコ", "old-model": "ねこ"},
        )

    def test_lines_split_by_newlines_or_br_and_a_line_without_a_name_ignored(self):
        parsed = op.parse_transcripts("a: あ\nb : い<br/>no name here<br>")
        self.assertEqual(parsed, {"a": "あ", "b": "い"})

    def test_the_clip_a_field_names(self):
        self.assertEqual(op.audio_file("[sound:a b.opus][sound:c.mp3]"), "a b.opus")
        self.assertIsNone(op.audio_file(""))

    def test_the_transcribers_whose_line_a_field_lacks(self):
        heard = op.parse_transcripts("ruby-mora: ネコ<br>old-model: ねこ")
        models = ["local-kana-anime-whisper", "local-ruby-mora", "gemini-x"]
        self.assertEqual(op.lacking(models, heard), ["local-kana-anime-whisper", "gemini-x"])
        self.assertEqual(op.lacking(models[1:2], heard), [])

    def test_a_local_transcribers_line_is_named_for_its_model(self):
        self.assertEqual(op.label("local-ruby-mora"), "ruby-mora")
        self.assertEqual(op.label("gemini-x"), "gemini-x")


class TranscribeNoteTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.media = Path(directory.name)
        (self.media / "clip.opus").write_bytes(CLIP)
        self.asked: list[tuple] = []
        self.answers = {"local-kana-anime-whisper": "ねこ", "local-ruby-mora": "ネコ"}

        def get_transcript(model, audio, *, kind, inputs, cancel_state=None):
            self.asked.append((model, audio, kind, inputs))
            text = self.answers.get(model)
            return None if text is None else {"text": text}

        patcher = mock.patch.object(op, "get_transcript", get_transcript)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reports: list[str] = []
        deliver = run_errors._deliver
        run_errors.deliver_with(lambda _title, text: self.reports.append(text))
        self.addCleanup(run_errors.deliver_with, deliver)

    def transcribe(self, note: Note) -> tuple[bool, dict]:
        updated: dict = {}
        changed = op.transcribe_note(CONFIG, note, {}, updated, media=self.media)
        return changed, updated

    def test_each_transcriber_hears_the_clip_and_its_line_is_written(self):
        note = Note({"Audio": "[sound:clip.opus]", "Heard": ""})
        changed, updated = self.transcribe(note)
        self.assertTrue(changed)
        self.assertIs(updated[7], note)
        self.assertEqual(note["Heard"], "kana-anime-whisper: ねこ<br>ruby-mora: ネコ")
        sha1 = hashlib.sha1(CLIP).hexdigest()
        self.assertEqual(
            self.asked,
            [
                (
                    "local-kana-anime-whisper",
                    CLIP,
                    "transcribe.audio",
                    {"audio": "clip.opus", "sha1": sha1, "model": "local-kana-anime-whisper"},
                ),
                (
                    "local-ruby-mora",
                    CLIP,
                    "transcribe.audio",
                    {"audio": "clip.opus", "sha1": sha1, "model": "local-ruby-mora"},
                ),
            ],
        )
        # What a replay's cassette is keyed by is plain JSON, the clip's bytes not in it
        json.dumps([inputs for *_, inputs in self.asked])

    def test_a_note_with_every_line_is_skipped_and_one_missing_is_asked_alone(self):
        done = Note({"Audio": "[sound:clip.opus]", "Heard": "kana-anime-whisper: x<br>ruby-mora: y"})
        self.assertEqual(self.transcribe(done), (False, {}))
        half = Note({"Audio": "[sound:clip.opus]", "Heard": "ruby-mora: y"})
        self.assertTrue(self.transcribe(half)[0])
        self.assertEqual([model for model, *_ in self.asked], ["local-kana-anime-whisper"])
        self.assertEqual(half["Heard"], "kana-anime-whisper: ねこ<br>ruby-mora: y")

    def test_what_came_is_kept_when_a_transcriber_fails(self):
        del self.answers["local-ruby-mora"]
        note = Note({"Audio": "[sound:clip.opus]", "Heard": ""})
        self.assertTrue(self.transcribe(note)[0])
        self.assertEqual(note["Heard"], "kana-anime-whisper: ねこ")

    def test_nothing_is_written_when_no_transcriber_answers(self):
        self.answers.clear()
        note = Note({"Audio": "[sound:clip.opus]", "Heard": ""})
        self.assertEqual(self.transcribe(note), (False, {}))
        self.assertEqual(note["Heard"], "")

    def test_a_note_without_a_clip_is_left_alone(self):
        self.assertEqual(self.transcribe(Note({"Audio": "", "Heard": ""})), (False, {}))
        self.assertEqual((self.asked, self.reports), ([], []))

    def test_a_clip_missing_from_the_media_folder_is_reported(self):
        note = Note({"Audio": "[sound:gone.opus]", "Heard": ""})
        self.assertEqual(self.transcribe(note), (False, {}))
        self.assertEqual(self.asked, [])
        self.assertIn("gone.opus", self.reports[0])

    def test_a_type_without_the_fields_configured_is_reported(self):
        config = {SENTENCE: {"word_list_field": "Words"}}
        changed = op.transcribe_note(config, Note({}), {}, {}, media=self.media)
        self.assertFalse(changed)
        self.assertIn("sentence_audio_field", self.reports[0])


class Response:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class Session:
    def __init__(self, answer) -> None:
        self.answer = answer
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if isinstance(self.answer, BaseException):
            raise self.answer
        if isinstance(self.answer, Response):
            return self.answer
        return Response(json.dumps(self.answer))


PROXY = "http://proxy.example:3128"


def proxy_env(**proxies: str):
    """The environment with these proxy settings in place of the machine's own (any `*_proxy`
    variable), the rest of it kept."""
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    return mock.patch.dict(os.environ, {**env, **proxies}, clear=True)


class ServerCheckTest(unittest.TestCase):
    def check(self, answer, models=CONFIG["transcribe_models"], config=None):
        session = Session(answer)
        with mock.patch.object(op, "get_session", lambda provider: session):
            problem = op.server_problem(config or {}, models)
        return problem, session.urls

    def test_a_server_with_every_model_loaded(self):
        loaded = {"models": {"kana-anime-whisper": {}, "ruby-mora": {}}}
        problem, urls = self.check(loaded, config={"transcribe_url": "http://box:9000/"})
        self.assertIsNone(problem)
        self.assertEqual(urls, ["http://box:9000/health"])

    def test_a_server_that_does_not_answer(self):
        problem, _ = self.check(ConnectionError("refused"))
        self.assertIn("does not answer", problem)
        self.assertIn("http://127.0.0.1:8792", problem)
        # Where the server is, which the released add-on leaves out with dev/
        self.assertIn("dev/transcribe_server.py of https://github.com/jhhr/anki_addons", problem)

    def test_a_proxy_that_cannot_reach_the_server(self):
        # HTTP_PROXY set and its proxy down, or refusing 127.0.0.1: requests raises ProxyError
        with mock.patch.object(op, "proxy_for", return_value=PROXY):
            problem, _ = self.check(requests.exceptions.ProxyError("Unable to connect to proxy"))
        self.assertIn(f"was asked through the proxy {PROXY}, which could not pass", problem)
        self.assertIn("a proxy seems to be in the way", problem)
        self.assertIn("Add 127.0.0.1 to the NO_PROXY environment variable", problem)
        self.assertIn('settings, where "<local>" covers localhost but not 127.0.0.1.', problem)
        with mock.patch.object(op, "proxy_for", return_value=PROXY):
            problem, _ = self.check(
                requests.exceptions.ProxyError("refused"),
                config={"transcribe_url": "http://localhost:8792"},
            )
        self.assertTrue(problem.endswith("exceptions of the system's proxy settings."))

    def test_a_proxys_error_page_in_the_servers_place(self):
        # A Squid-like proxy answers a request for 127.0.0.1 with a page of its own
        page = Response("<html><body><h1>ERROR</h1>Access Denied.</body></html>", 403)
        with mock.patch.object(op, "proxy_for", return_value=PROXY):
            problem, _ = self.check(page)
        self.assertIn(f"through the proxy {PROXY}, which answered HTTP 403 instead", problem)
        self.assertIn("NO_PROXY", problem)

    def test_an_answer_not_the_servers_with_no_proxy_is_not_put_on_one(self):
        with mock.patch.object(op, "proxy_for", return_value=None):
            problem, _ = self.check(Response("<html>Not Found</html>", 404))
        self.assertIn("does not answer (JSONDecodeError)", problem)
        self.assertNotIn("proxy", problem)

    def test_the_proxy_a_local_request_goes_through(self):
        url = "http://127.0.0.1:8792"
        with proxy_env(HTTP_PROXY=PROXY):
            self.assertEqual(op.proxy_for(url), PROXY)
        with proxy_env(HTTP_PROXY=PROXY, NO_PROXY="localhost,127.0.0.1"):
            self.assertIsNone(op.proxy_for(url))
        # localhost alone does not cover 127.0.0.1, nor HTTPS_PROXY a request over http
        with proxy_env(HTTP_PROXY=PROXY, NO_PROXY="localhost"):
            self.assertEqual(op.proxy_for(url), PROXY)
        with proxy_env(HTTPS_PROXY=PROXY):
            self.assertIsNone(op.proxy_for(url))

    def test_a_server_without_a_model(self):
        problem, _ = self.check({"models": {"ruby-mora": {}}})
        self.assertIn("has not loaded kana-anime-whisper", problem)

    def test_no_local_transcriber_needs_no_server(self):
        self.assertEqual(self.check(ConnectionError(), models=["gemini-x"]), (None, []))


class SameFieldTest(unittest.TestCase):
    def test_a_transcript_field_that_is_the_audio_field_is_refused_once_touching_nothing(self):
        # Written there, the transcripts would replace the clip's [sound:] tag
        config = {**CONFIG, SENTENCE: {**CONFIG[SENTENCE], "sentence_transcription_field": "Audio"}}
        reports: list[str] = []
        previous = run_errors.deliver_with(lambda _title, text: reports.append(text))
        self.addCleanup(run_errors.deliver_with, previous)
        ran: list = []

        async def bulk_notes_op(message, config, note_op, col, notes, *args, **kwargs):
            ran.extend(note.id for note in notes)
            return "ran"

        notes = [Note({"Audio": CLIP_TAG}, note_id=1), Note({"Audio": CLIP_TAG}, note_id=2)]
        col = mock.Mock()
        col.media.dir.return_value = "media"
        with mock.patch.object(op, "mw") as mw, mock.patch.multiple(
            op, bulk_notes_op=bulk_notes_op, server_problem=mock.Mock(return_value="down")
        ):
            mw.addonManager.getConfig.return_value = config
            asyncio.run(op.bulk_transcribe_notes_op(col, notes, [], mock.Mock(), {}, {}))
        self.assertEqual(ran, [])
        [text] = reports
        self.assertIn("sentence_transcription_field is the sentence_audio_field", text)
        self.assertIn("2 notes", text)
        self.assertEqual([note["Audio"] for note in notes], [CLIP_TAG, CLIP_TAG])

    def test_two_fields_or_none_configured_are_no_such_error(self):
        self.assertIsNone(op.same_field_error(CONFIG, SENTENCE))
        self.assertIsNone(op.same_field_error({SENTENCE: {}}, SENTENCE))


class ProxyInTheWayTest(unittest.TestCase):
    """The real session, sent through stand-in proxies on 127.0.0.1 by HTTP_PROXY: what a proxy
    in the way makes of the check, as requests reports it."""

    def problem(self, proxy: str) -> str:
        with proxy_env(HTTP_PROXY=proxy):
            config = {"transcribe_url": "http://127.0.0.1:9"}
            return op.server_problem(config, CONFIG["transcribe_models"])

    def test_a_proxy_that_is_down(self):
        # Nothing listens on the port once it is closed
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        problem = self.problem(f"http://127.0.0.1:{port}")
        self.assertIn(f"through the proxy http://127.0.0.1:{port}, which could not pass", problem)

    def test_a_proxy_that_refuses_local_requests(self):
        class Denying(BaseHTTPRequestHandler):
            """As Squid's default configuration answers a request for 127.0.0.1."""

            def do_GET(self):
                body = b"<html><body><h1>ERROR</h1>Access Denied.</body></html>"
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Denying)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        problem = self.problem(f"http://127.0.0.1:{server.server_address[1]}")
        self.assertIn("which answered HTTP 403 instead of the server", problem)


class BulkServerCheckTest(unittest.TestCase):
    def run_bulk(self, notes):
        """The bulk op with the server down: the notes it went on with, and what it reported."""
        ran: list = []

        async def bulk_notes_op(message, config, op, col, notes, *args, **kwargs):
            ran.extend(note.id for note in notes)
            return "ran"

        col = mock.Mock()
        col.media.dir.return_value = "media"
        with mock.patch.object(op, "mw") as mw, mock.patch.multiple(
            op,
            bulk_notes_op=bulk_notes_op,
            server_problem=mock.Mock(return_value="The server does not answer"),
            report_error=mock.Mock(),
        ):
            mw.addonManager.getConfig.return_value = CONFIG
            result = asyncio.run(op.bulk_transcribe_notes_op(col, notes, [], mock.Mock(), {}, {}))
            return result, ran, op.report_error.call_args_list

    def test_notes_transcribed_already_need_no_server(self):
        both = "kana-anime-whisper: たのひとわ<br>ruby-mora: タノヒトワ"
        result, ran, reported = self.run_bulk([Note({"Audio": CLIP_TAG, "Heard": both}, 1)])
        self.assertEqual((result, ran, reported), ("ran", [1], []))

    def test_a_note_without_a_clip_needs_no_server(self):
        # One added by hand: no clip, so no line either, and nothing to send
        both = "kana-anime-whisper: たのひとわ<br>ruby-mora: タノヒトワ"
        notes = [Note({"Audio": CLIP_TAG, "Heard": both}, 1), Note({"Audio": "", "Heard": ""}, 2)]
        result, ran, reported = self.run_bulk(notes)
        self.assertEqual((result, ran, reported), ("ran", [1, 2], []))

    def test_a_note_lacking_a_line_stops_the_run_when_the_server_is_down(self):
        notes = [Note({"Audio": CLIP_TAG, "Heard": "kana-anime-whisper: たのひとわ"}, note_id=2)]
        result, ran, reported = self.run_bulk(notes)
        self.assertEqual((result, ran), (None, []))
        self.assertEqual(reported[0].args[0], "The server does not answer")

    def test_a_responder_hears_the_clips_without_a_server(self):
        base_ops = load_ops_module("base_ops")
        previous = base_ops.set_responder(lambda request: {"text": "たのひとわ"})
        try:
            notes = [Note({"Audio": CLIP_TAG, "Heard": "kana-anime-whisper: たのひとわ"}, 2)]
            result, ran, reported = self.run_bulk(notes)
        finally:
            base_ops.set_responder(previous)
        # The server is down in run_bulk: had it been asked, the run would have stopped
        self.assertEqual((result, ran, reported), ("ran", [2], []))

    def test_a_line_lacking_is_one_of_the_configs_transcribers(self):
        self.assertTrue(op.lacks_transcript(CONFIG, Note({"Audio": CLIP_TAG, "Heard": ""})))
        every = "x: a<br>ruby-mora: b<br>kana-anime-whisper: c"
        self.assertFalse(op.lacks_transcript(CONFIG, Note({"Audio": CLIP_TAG, "Heard": every})))
        # One a note without a clip could never get
        self.assertFalse(op.lacks_transcript(CONFIG, Note({"Audio": "", "Heard": ""})))
        # A field the note lacks is asked about, and the op says why it cannot be
        self.assertTrue(op.lacks_transcript(CONFIG, Note({})))
        self.assertTrue(op.lacks_transcript(CONFIG, Note({"Heard": ""})))
        # So is one a hand-edited config names by a list, which raised TypeError before a run
        listed = {SENTENCE: {**CONFIG[SENTENCE], "sentence_transcription_field": ["Heard"]}}
        self.assertTrue(op.lacks_transcript(listed, Note({"Heard": every})))


if __name__ == "__main__":
    unittest.main()
