"""Minimal JMdict index: written form (kanji or kana) -> entries' readings and POS codes.

Used to find multi-word units the tokenizer leaves apart (そう言えば, 鳥肌が立つ, 方が良い) and to
pick dictionary-form readings. JMdict_e.gz is downloaded into user_files/jmdict/ by
resources.ensure(); the parsed index is pickled next to it.
"""

import gzip
import pickle
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

JMDICT_URL = "https://www.edrdg.org/pub/Nihongo/JMdict_e.gz"
DATA_DIR = Path(__file__).resolve().parent.parent / "user_files" / "jmdict"
JMDICT_GZ = DATA_DIR / "JMdict_e.gz"
INDEX_PICKLE = DATA_DIR / "jmdict_index.pkl"
# Bumped whenever the index's shape changes, so an old pickle is rebuilt instead of misread
INDEX_FORMAT = 1

Entry = tuple[tuple[str, ...], frozenset[str]]  # (readings, POS codes like "n", "exp", "v5r")

# JMdict writes POS as DTD entities (&n;, &v5r;). Replaced by their names before parsing, as
# the codes are what the index keeps; the DTD itself is skipped.
ENTITY_RE = re.compile(r"&([\w.-]+);")


def is_available() -> bool:
    return _load_pickle() is not None or JMDICT_GZ.exists()


def _load_pickle():
    try:
        fmt, idx = pickle.loads(INDEX_PICKLE.read_bytes())
    except (OSError, ValueError, TypeError, pickle.UnpicklingError, EOFError):
        return None
    return idx if fmt == INDEX_FORMAT else None


def build(gz_path: Path = JMDICT_GZ) -> dict[str, list[Entry]]:
    """Parse JMdict_e.gz entry by entry, never holding the 60 MB of XML in memory at once."""
    index: dict[str, list[Entry]] = {}
    parser = ET.XMLPullParser(events=("end",))
    in_body = False
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        for line in f:
            if not in_body:
                start = line.find("<JMdict>")
                if start < 0:
                    continue
                line, in_body = line[start:], True
            parser.feed(ENTITY_RE.sub(r"\1", line))
            for _, el in parser.read_events():
                if el.tag != "entry":
                    continue
                kebs = [k.text for k in el.iter("keb") if k.text]
                rebs = tuple(r.text for r in el.iter("reb") if r.text)
                pos = frozenset(p.text for p in el.iter("pos") if p.text)
                for form in kebs + list(rebs):
                    index.setdefault(form, []).append((rebs, pos))
                el.clear()
    parser.close()
    return index


@lru_cache(maxsize=1)
def index() -> dict[str, list[Entry]]:
    idx = _load_pickle()
    if idx is not None:
        return idx
    if not JMDICT_GZ.exists():
        raise FileNotFoundError(
            f"JMdict not found at {JMDICT_GZ}; resources.ensure() downloads it"
            " (from a script: word_array/research/setup_resources.py)"
        )
    idx = build(JMDICT_GZ)
    INDEX_PICKLE.write_bytes(pickle.dumps((INDEX_FORMAT, idx), protocol=pickle.HIGHEST_PROTOCOL))
    return idx


def lookup(form: str) -> list[Entry]:
    return index().get(form, [])


def has_pos(form: str, code: str) -> bool:
    """Whether any entry for form has a POS code equal to or starting with code ("v5" etc.)."""
    return any(code in ps or any(p.startswith(code) for p in ps) for _, ps in lookup(form))


def readings(form: str) -> list[str]:
    return [r for rs, _ in lookup(form) for r in rs]
