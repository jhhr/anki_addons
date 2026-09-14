"""The hand judge's note access: export note ids and the AnkiConnect client, with a fake opener."""

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

from addon_modules import ADDON_ROOT

RESEARCH = ADDON_ROOT / "word_array" / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import anki_connect  # noqa: E402
import hand_labels  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def client_replying(reply: dict) -> tuple[anki_connect.AnkiConnect, list[dict]]:
    requests: list[dict] = []

    def opener(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(json.dumps(reply).encode("utf-8"))

    return anki_connect.AnkiConnect(opener=opener), requests


class AnkiConnectTests(unittest.TestCase):
    def test_a_request_names_the_action_version_and_params(self):
        info = [{"noteId": 5, "modelName": "m", "fields": {}}]
        client, requests = client_replying({"result": info, "error": None})
        self.assertEqual(client.notes_info([5]), info)
        self.assertEqual(
            requests, [{"action": "notesInfo", "version": 6, "params": {"notes": [5]}}]
        )

    def test_update_note_fields_sends_the_note(self):
        client, requests = client_replying({"result": None, "error": None})
        client.update_note_fields(5, {"f": "新しい"})
        self.assertEqual(requests[0]["params"], {"note": {"id": 5, "fields": {"f": "新しい"}}})

    def test_browse_every_note_of_a_sentence(self):
        client, requests = client_replying({"result": [1], "error": None})
        client.gui_browse(anki_connect.nids_query([1, 2]))
        self.assertEqual(requests[0]["params"], {"query": "nid:1 or nid:2"})

    def test_an_action_error_raises(self):
        client, _ = client_replying({"result": None, "error": "note was not found: 5"})
        with self.assertRaisesRegex(anki_connect.AnkiConnectError, "note was not found"):
            client.update_note_fields(5, {"f": ""})

    def test_anki_not_running_says_so(self):
        def refused(request, timeout):
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

        client = anki_connect.AnkiConnect(opener=refused)
        with self.assertRaisesRegex(anki_connect.AnkiConnectError, "is Anki running"):
            client.version()


class ConfigTests(unittest.TestCase):
    def test_meta_config_over_config_json_names_the_sentence_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps({"Note": {"word_extraction_sentence_field": "old"}}), encoding="utf-8"
            )
            (root / "meta.json").write_text(
                json.dumps({"config": {"Note": {"word_extraction_sentence_field": "s"}}}),
                encoding="utf-8",
            )
            config = anki_connect.load_config(root)
        self.assertEqual(anki_connect.sentence_field(config, "Note"), "s")
        info = {
            "noteId": 1,
            "modelName": "Note",
            "fields": {"s": {"value": "猫[ねこ]", "order": 0}},
        }
        self.assertEqual(anki_connect.note_sentence(config, info), "猫[ねこ]")
        with self.assertRaisesRegex(anki_connect.AnkiConnectError, "Other"):
            anki_connect.sentence_field(config, "Other")
        with self.assertRaisesRegex(anki_connect.AnkiConnectError, "no field"):
            anki_connect.note_sentence(config, {"noteId": 2, "modelName": "Note", "fields": {}})


class ExportNidsTests(unittest.TestCase):
    def test_note_ids_by_raw_sentence(self):
        rows = [
            {"sentence": "<i>前</i>猫[ねこ]", "word_list": "{}", "nids": [1, 2]},
            {"sentence": "犬[いぬ]", "word_list": "{}"},
            {"sentence": "<i>前</i>猫[ねこ]", "word_list": "{}", "nids": [2, 3]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.jsonl"
            text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n\n"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(hand_labels.read_export_nids(path), {"<i>前</i>猫[ねこ]": [1, 2, 3]})
            self.assertEqual(hand_labels.read_export_nids(Path(tmp) / "missing.jsonl"), {})


if __name__ == "__main__":
    unittest.main()
