"""Reading and editing Anki notes from a research script, through the AnkiConnect add-on.

AnkiConnect listens on 127.0.0.1:8765 only, so a page opened from a phone can't call it: the
script's server makes the calls in Python for it. A request sent from Python carries no Origin
header, which AnkiConnect lets through whatever its CORS list says.

`updateNoteFields` skips Anki's undo queue, so an edit can't be undone in Anki; whoever edits
keeps the old value to write back.

The sentence field is the add-on's `word_extraction_sentence_field` for the note's type, read
from config.json with meta.json's config over it, as `judge_eval.py run` reads the config.
"""

import json
import urllib.request
from pathlib import Path
from typing import Callable, Sequence

from _bootstrap import ADDON_ROOT

URL = "http://127.0.0.1:8765"
VERSION = 6
SENTENCE_FIELD = "word_extraction_sentence_field"


class AnkiConnectError(Exception):
    pass


class AnkiConnect:
    def __init__(self, url: str = URL, opener: Callable = urllib.request.urlopen, timeout=10):
        self.url = url
        self.opener = opener
        self.timeout = timeout

    def invoke(self, action: str, **params):
        """The action's result; AnkiConnectError when Anki can't be reached or the action fails."""
        body = json.dumps({"action": action, "version": VERSION, "params": params}).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                reply = json.loads(response.read().decode("utf-8"))
        except OSError as e:
            raise AnkiConnectError(
                f"No answer from AnkiConnect at {self.url} ({e}): is Anki running with AnkiConnect?"
            ) from e
        except ValueError as e:
            raise AnkiConnectError(f"{action}: unreadable reply from {self.url} ({e})") from e
        if not isinstance(reply, dict) or "error" not in reply or "result" not in reply:
            raise AnkiConnectError(f"{action}: unexpected reply {reply!r}")
        if reply["error"] is not None:
            raise AnkiConnectError(f"{action}: {reply['error']}")
        return reply["result"]

    def version(self) -> int:
        return self.invoke("version")

    def notes_info(self, nids: Sequence[int]) -> list[dict]:
        """`{noteId, modelName, fields: {name: {value, order}}, tags, ...}` per id, `{}` for an id
        no note has."""
        return self.invoke("notesInfo", notes=list(nids))

    def update_note_fields(self, nid: int, fields: dict[str, str]) -> None:
        self.invoke("updateNoteFields", note={"id": nid, "fields": fields})

    def add_tags(self, nids, tags: str) -> None:
        """Adds the space separated `tags` to every note; a tag a note already has is left be."""
        self.invoke("addTags", notes=list(nids), tags=tags)

    def remove_tags(self, nids, tags: str) -> None:
        self.invoke("removeTags", notes=list(nids), tags=tags)

    def gui_browse(self, query: str) -> list[int]:
        """Opens Anki's browser on `query`; the ids of the cards it found."""
        return self.invoke("guiBrowse", query=query)


def nids_query(nids: Sequence[int]) -> str:
    return " or ".join(f"nid:{nid}" for nid in nids)


def load_config(root: Path = ADDON_ROOT) -> dict:
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    meta = root / "meta.json"
    if meta.exists():
        config.update(json.loads(meta.read_text(encoding="utf-8")).get("config", {}))
    return config


def sentence_field(config: dict, model_name: str, key: str = SENTENCE_FIELD) -> str:
    field = (config.get(model_name) or {}).get(key)
    if not field:
        raise AnkiConnectError(f'Note type "{model_name}" has no {key} in the config.')
    return field


def note_sentence(config: dict, info: dict, key: str = SENTENCE_FIELD) -> str:
    """The raw sentence field (config `key`) of a `notes_info` note, `<i>` context and all."""
    field = sentence_field(config, info.get("modelName", ""), key)
    try:
        return info["fields"][field]["value"]
    except KeyError:
        raise AnkiConnectError(f'Note {info.get("noteId")} has no field "{field}".') from None
