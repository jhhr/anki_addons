"""Minimal JMdict index: written form (kanji or kana) -> entries' readings and POS codes.

Used to find multi-word units the tokenizer leaves apart (そう言えば, 鳥肌が立つ, 方が良い) and to
pick dictionary-form readings. JMdict_e is downloaded once into user_files/jmdict/ (not
committed); the parsed index is pickled next to it.
"""

import gzip
import pickle
import re
import shutil
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

JMDICT_URL = "http://ftp.edrdg.org/pub/Nihongo/JMdict_e.gz"
DATA_DIR = Path(__file__).resolve().parent.parent / "user_files" / "jmdict"
JMDICT_XML = DATA_DIR / "JMdict_e.xml"
INDEX_PICKLE = DATA_DIR / "jmdict_index.pkl"

Entry = tuple[tuple[str, ...], frozenset[str]]  # (readings, POS codes like "n", "exp", "v5r")


def is_available() -> bool:
    return INDEX_PICKLE.exists() or JMDICT_XML.exists()


def download() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    gz_path = DATA_DIR / "JMdict_e.gz"
    urllib.request.urlretrieve(JMDICT_URL, gz_path)
    with gzip.open(gz_path, "rb") as src, open(JMDICT_XML, "wb") as dst:
        shutil.copyfileobj(src, dst)
    gz_path.unlink()
    return JMDICT_XML


def _build(xml_path: Path) -> dict[str, list[Entry]]:
    text = xml_path.read_text(encoding="utf-8")
    text = text[text.index("<JMdict>") :]  # drop the DTD
    text = re.sub(r"&([\w.-]+);", r"\1", text)  # keep entity codes (n, exp, v5r) as plain text
    index: dict[str, list[Entry]] = {}
    for entry in ET.fromstring(text).iter("entry"):
        kebs = [k.text for k in entry.iter("keb") if k.text]
        rebs = tuple(r.text for r in entry.iter("reb") if r.text)
        pos = frozenset(p.text for p in entry.iter("pos") if p.text)
        for form in kebs + list(rebs):
            index.setdefault(form, []).append((rebs, pos))
    return index


@lru_cache(maxsize=1)
def index() -> dict[str, list[Entry]]:
    if INDEX_PICKLE.exists():
        return pickle.loads(INDEX_PICKLE.read_bytes())
    if not JMDICT_XML.exists():
        raise FileNotFoundError(
            f"JMdict not found at {JMDICT_XML}; run word_array/research/setup_jmdict.py"
        )
    idx = _build(JMDICT_XML)
    INDEX_PICKLE.write_bytes(pickle.dumps(idx))
    return idx


def lookup(form: str) -> list[Entry]:
    return index().get(form, [])


def has_pos(form: str, code: str) -> bool:
    """Whether any entry for form has a POS code equal to or starting with code ("v5" etc.)."""
    return any(code in ps or any(p.startswith(code) for p in ps) for _, ps in lookup(form))


def readings(form: str) -> list[str]:
    return [r for rs, _ in lookup(form) for r in rs]
