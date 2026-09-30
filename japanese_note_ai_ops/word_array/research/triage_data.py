"""What the vocab triage scripts share: where their files go, their settings, and the notes,
cards and review history they read from a copy of the collection.

The triage sorts the vocab notes `match_words_to_notes` created from the sentence corpus (tagged
`new_matched_jp_word`, which that op hardcodes) into three actions: suspend, schedule far out, or
leave new to learn. Every script reads a copy of the collection, never the profile's, which Anki
keeps open: `triage_extract.py restore` makes one from a backup. The copy is opened with the
`anki` library, as dev/headless.py opens one, and not through AnkiConnect, which takes minutes
over what one query does here over the whole revlog.

What the scripts write names the user's words, so it goes to `vocab_triage/` in the private test
data checkout's `japanese_note_ai_ops/evals/` (`_bootstrap.data_paths`), or to
`output/vocab_triage/` on a machine without one. So do the names of the user's note type and
decks: `settings.json` there holds them, written from `triage_extract.py`'s arguments, so that no
committed file has to.
"""

from __future__ import annotations

import html
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from _bootstrap import OUTPUT, data_paths

# match_words_to_notes puts it on every note it creates
NEW_WORD_TAG = "new_matched_jp_word"
# The collection's own (the addon's hooks hardcode "Kanji draw" too), for the kanji analysis
DEFAULT_KANJI_NOTE_TYPE = "Kanji draw"

SETTINGS_KEYS = (
    "collection",
    "note_type",
    "processing_deck",
    "learn_deck",
    "ignore_tag",
    "kanji_note_type",
)

TAGS_RE = re.compile(r"<[^>]+>")
MARKERS_RE = re.compile(r"(\s*\([^)]*\))+$")
MARKER_RE = re.compile(r"\(([^)]*)\)")
LINKED_NID_RE = re.compile(r"nid:(\d+)")

DAY_MS = 86_400_000


def triage_dir() -> Path:
    evals = data_paths().data_dir("evals")
    root = evals / "vocab_triage" if evals is not None else OUTPUT / "vocab_triage"
    root.mkdir(parents=True, exist_ok=True)
    return root


def data_file(name: str) -> Path:
    return triage_dir() / name


def report_file(name: str) -> Path:
    path = triage_dir() / "reports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_settings() -> dict:
    path = data_file("settings.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def settings(args: Any = None, required: Iterable[str] = ("collection", "note_type")) -> dict:
    """The settings, with any given on the command line (`args.<key>`) saved over them first.
    Exits naming the missing ones: a script run before `triage_extract.py` has none."""
    current = read_settings()
    changed = False
    for key in SETTINGS_KEYS:
        value = getattr(args, key, None) if args is not None else None
        if value is not None and current.get(key) != value:
            current[key] = value
            changed = True
    current.setdefault("ignore_tag", "fsrs_ignore")
    current.setdefault("kanji_note_type", DEFAULT_KANJI_NOTE_TYPE)
    if changed:
        data_file("settings.json").write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    missing = [key for key in required if not current.get(key)]
    if missing:
        sys.exit(
            "missing settings: %s; give them to triage_extract.py (--%s)"
            % (", ".join(missing), " --".join(k.replace("_", "-") for k in missing))
        )
    return current


def add_settings_args(parser: Any) -> None:
    """The settings as options, each saved when given."""
    parser.add_argument("--collection", help="the working copy (triage_extract.py restore)")
    parser.add_argument("--note-type", dest="note_type", help="the vocab note type's name")
    parser.add_argument("--processing-deck", dest="processing_deck",
                        help="the deck the new notes wait in")
    parser.add_argument("--learn-deck", dest="learn_deck", help="where the words to learn go")
    parser.add_argument("--ignore-tag", dest="ignore_tag",
                        help="the tag that keeps a card out of the FSRS parameters")
    parser.add_argument("--kanji-note-type", dest="kanji_note_type")


def open_collection(path: str):
    """The working copy, opened with the anki library. Nothing here writes to it."""
    from anki.collection import Collection

    return Collection(str(path))


def restore_backup(backup: Path, to: Path) -> None:
    """A .colpkg backup unpacked into a collection file at `to`, which must not exist yet."""
    from anki._backend import RustBackend

    if to.exists():
        raise FileExistsError(f"{to} exists: restore into a new path")
    to.parent.mkdir(parents=True, exist_ok=True)
    media = to.parent / (to.stem + ".media")
    media.mkdir(exist_ok=True)
    # A bare backend: a Collection's own holds the file open, and the import refuses that
    RustBackend().import_collection_package(
        col_path=str(to),
        backup_path=str(backup),
        media_folder=str(media),
        media_db=str(to.parent / (to.stem + ".media.db2")),
    )


def plain(text: str) -> str:
    text = TAGS_RE.sub("", text or "")
    return re.sub(r"\s+", " ", html.unescape(text).replace(" ", " ")).strip()


def base_form(key: str) -> str:
    """The sort field without its trailing `(m2)`, `(r1)`, `(kun)` markers (vocab_dupes')."""
    return MARKERS_RE.sub("", plain(key)).strip()


def markers(key: str) -> list[str]:
    key = plain(key)
    return MARKER_RE.findall(key[len(base_form(key)) :])


FURIGANA_RE = re.compile(r" ?([^ \[\]]+?)\[([^\]]*)\]")


def furigana_kana(text: str) -> str:
    """A furigana field read as kana: `引[ひ]き 続[つづ]く` -> `ひきつづく`."""
    return FURIGANA_RE.sub(lambda m: m.group(2), plain(text)).replace(" ", "")


@dataclass
class Card:
    cid: int
    nid: int
    did: int
    type: int
    queue: int
    due: int
    ivl: int
    reps: int
    lapses: int
    odid: int
    data: dict

    @property
    def suspended(self) -> bool:
        return self.queue == -1

    @property
    def is_new(self) -> bool:
        return self.type == 0

    @property
    def stability(self) -> Optional[float]:
        return self.data.get("s")

    @property
    def difficulty(self) -> Optional[float]:
        return self.data.get("d")

    @property
    def decay(self) -> float:
        return self.data.get("decay") or 0.5

    @property
    def last_review_s(self) -> Optional[int]:
        """When the card was last reviewed, seconds since the epoch (FSRS's `lrt`)."""
        return self.data.get("lrt")


@dataclass
class VocabNote:
    nid: int
    mid: int
    tags: list[str]
    fields: dict[str, str]
    cards: list[Card] = field(default_factory=list)

    def get(self, name: str) -> str:
        return self.fields.get(name, "")

    @property
    def key(self) -> str:
        return plain(self.get("vocab-key"))

    @property
    def base(self) -> str:
        return base_form(self.get("vocab-key"))

    @property
    def reading(self) -> str:
        return plain(self.get("vocab-kana"))

    @property
    def kanjified(self) -> str:
        return plain(self.get("vocab-kanjified"))

    @property
    def card(self) -> Optional[Card]:
        return self.cards[0] if self.cards else None

    @property
    def reviewed(self) -> bool:
        """Whether any card of it has been answered at least once."""
        return any(c.reps > 0 for c in self.cards)

    def has_tag(self, tag: str) -> bool:
        return tag.lower() in (t.lower() for t in self.tags)


def forgetting_curve(elapsed_days: float, stability: float, decay: float) -> float:
    """FSRS's recall probability after `elapsed_days` at `stability`, with its decay (FSRS-6
    stores a per-preset decay; 0.5 was FSRS-4.5's fixed one)."""
    if stability <= 0:
        return 0.0
    factor = 0.9 ** (-1.0 / decay) - 1.0
    return (1.0 + factor * max(elapsed_days, 0.0) / stability) ** (-decay)


def retrievability(card: Card, now_s: float) -> Optional[float]:
    if card.stability is None or card.last_review_s is None:
        return None
    return forgetting_curve((now_s - card.last_review_s) / 86400.0, card.stability, card.decay)


def load_cards(col: Any, nids: Optional[Iterable[int]] = None) -> dict[int, list[Card]]:
    """Cards by note id, of `nids` or of every note."""
    by_note: dict[int, list[Card]] = defaultdict(list)
    sql = "select id, nid, did, type, queue, due, ivl, reps, lapses, odid, data from cards"
    # The row's shape is spelled out so the checker sees `row[:10]` stop short of `data`
    rows: Iterable[tuple[int, int, int, int, int, int, int, int, int, int, str]]
    if nids is None:
        rows = col.db.all(sql)
    else:
        rows = (r for chunk in chunked(list(nids), 900)
                for r in col.db.all(sql + " where nid in (%s)" % ",".join(map(str, chunk))))
    for row in rows:
        try:
            data = json.loads(row[10]) if row[10] else {}
        except ValueError:
            data = {}
        by_note[row[1]].append(Card(*row[:10], data=data))
    for cards in by_note.values():
        cards.sort(key=lambda c: c.cid)
    return by_note


def load_vocab_notes(col: Any, note_type: str) -> dict[int, VocabNote]:
    """Every note of the vocab note type with its cards, by note id."""
    model = col.models.by_name(note_type)
    if model is None:
        sys.exit(f"no note type named {note_type!r} in the collection")
    names = [f["name"] for f in model["flds"]]
    notes: dict[int, VocabNote] = {}
    for nid, tags, flds in col.db.all(
        "select id, tags, flds from notes where mid = ?", model["id"]
    ):
        values = flds.split("\x1f")
        notes[nid] = VocabNote(nid, model["id"], tags.split(), dict(zip(names, values)))
    cards = load_cards(col)
    for nid, note in notes.items():
        note.cards = cards.get(nid, [])
    return notes


@dataclass
class Review:
    id: int  # ms since the epoch, the review's time
    cid: int
    ease: int  # 1-4 the button; 0 for a manual entry
    ivl: int  # days when positive, seconds when negative
    last_ivl: int
    factor: int
    time_ms: int
    type: int  # 0 learn, 1 review, 2 relearn, 3 filtered, 4 manual, 5 rescheduled


REVLOG_LEARN, REVLOG_REVIEW, REVLOG_RELEARN, REVLOG_FILTERED, REVLOG_MANUAL, REVLOG_RESCHED = range(6)


def load_revlog(col: Any, cids: Optional[Iterable[int]] = None) -> dict[int, list[Review]]:
    """Review history by card id, oldest first."""
    by_card: dict[int, list[Review]] = defaultdict(list)
    sql = "select id, cid, ease, ivl, lastIvl, factor, time, type from revlog"
    rows: Iterable
    if cids is None:
        rows = col.db.all(sql + " order by id")
    else:
        rows = (r for chunk in chunked(list(cids), 900)
                for r in col.db.all(sql + " where cid in (%s) order by id"
                                    % ",".join(map(str, chunk))))
    for row in rows:
        by_card[row[1]].append(Review(*row))
    for reviews in by_card.values():
        reviews.sort(key=lambda r: r.id)
    return by_card


def chunked(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def linked_nids(text: str) -> list[int]:
    return [int(n) for n in LINKED_NID_RE.findall(text or "")]


MEANING, READING, SPELLING = "meaning", "reading", "spelling"


def siblings(notes: dict[int, VocabNote]) -> dict[int, dict[int, str]]:
    """For each note, the notes of the same word and how they differ from it.

    - `meaning`: the same word in another sense: the same sort-field base and reading, the same
      `vocab-kanjified` and `vocab-kana` (the op's lookup key), or linked in `other-meanings`,
      which also links a sense whose note is keyed in kana (`つける (m2)` beside `付ける (r1)(m1)`).
    - `reading`: the same sort-field base read otherwise, the `(rN)` notes.
    - `spelling`: linked in `other-readings`, which links another word written with the same
      kanji (過ぎる and よぎる).
    """
    by_base: dict[str, list[int]] = defaultdict(list)
    by_lookup: dict[tuple, list[int]] = defaultdict(list)
    for nid, note in notes.items():
        by_base[note.base].append(nid)
        if note.kanjified and note.reading:
            by_lookup[(note.kanjified, note.reading)].append(nid)
    out: dict[int, dict[int, str]] = {}
    for nid, note in notes.items():
        found: dict[int, str] = {}
        for other in by_base.get(note.base, []):
            if other != nid:
                found[other] = MEANING if notes[other].reading == note.reading else READING
        for other in by_lookup.get((note.kanjified, note.reading), []):
            if other != nid:
                found[other] = MEANING
        for other in linked_nids(note.get("other-meanings")):
            if other in notes and other != nid:
                found[other] = MEANING
        for other in linked_nids(note.get("other-readings")):
            if other in notes and other != nid:
                found.setdefault(other, SPELLING)
        out[nid] = found
    return out


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    n = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(path)
    return n


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def log10_or_none(value: Optional[float]) -> Optional[float]:
    return math.log10(value) if value and value > 0 else None
