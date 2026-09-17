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
from typing import Optional

JMDICT_URL = "https://www.edrdg.org/pub/Nihongo/JMdict_e.gz"
DATA_DIR = Path(__file__).resolve().parent.parent / "user_files" / "jmdict"
JMDICT_GZ = DATA_DIR / "JMdict_e.gz"
INDEX_PICKLE = DATA_DIR / "jmdict_index.pkl"
# Bumped whenever the index's shape changes, so an old pickle is rebuilt instead of misread
INDEX_FORMAT = 5

# (kanji spellings, readings, POS codes like "n", "exp", "v5r"). Plain tuples: a NamedTuple
# would pickle under this module's package name, which differs between Anki and the scripts.
Entry = tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]

# What JMdict says about an entry's spellings: the ke_inf codes of its kanji spellings (oK
# outdated: 爲に, sK search-only: 御願い, rK rarely used: 抔), plus COMMON for one with a ke_pri;
# the readings that go with only some spellings (re_restr; none at all for re_nokanji); and
# whether it is usually written in kana (uk, in any sense). Keyed by (kebs, rebs).
Spellings = tuple[dict[str, frozenset[str]], dict[str, tuple[str, ...]], bool]
NO_SPELLINGS: Spellings = ({}, {}, False)
COMMON = "common"

# JMdict writes POS as DTD entities (&n;, &v5r;). Replaced by their names before parsing, as
# the codes are what the index keeps; the DTD itself is skipped.
ENTITY_RE = re.compile(r"&([\w.-]+);")


def is_available() -> bool:
    return _load_pickle() is not None or JMDICT_GZ.exists()


def _load_pickle():
    try:
        fmt, *data = pickle.loads(INDEX_PICKLE.read_bytes())
    except (OSError, ValueError, TypeError, pickle.UnpicklingError, EOFError):
        return None
    return tuple(data) if fmt == INDEX_FORMAT else None


def _kanji_info(el: ET.Element) -> dict[str, frozenset[str]]:
    info = {}
    for k in el.iter("k_ele"):
        codes = {i.text for i in k.iter("ke_inf")} | (
            {COMMON} if k.find("ke_pri") is not None else set()
        )
        if codes:
            info[k.findtext("keb")] = frozenset(codes)
    return info


def _reading_restrictions(el: ET.Element) -> dict[str, tuple[str, ...]]:
    restr = {}
    for r in el.iter("r_ele"):
        if r.find("re_nokanji") is not None:
            restr[r.findtext("reb")] = ()
        elif r.find("re_restr") is not None:
            restr[r.findtext("reb")] = tuple(x.text for x in r.iter("re_restr"))
    return restr


def build(gz_path: Path = JMDICT_GZ, spellings: Optional[dict] = None) -> dict[str, list[Entry]]:
    """Parse JMdict_e.gz entry by entry, never holding the 60 MB of XML in memory at once.
    `spellings`, when given, is filled with the `Spellings` of every entry that has any."""
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
                if not isinstance(el, ET.Element) or el.tag != "entry":
                    continue
                kebs = tuple(k.text for k in el.iter("keb") if k.text)
                rebs = tuple(r.text for r in el.iter("reb") if r.text)
                pos = frozenset(p.text for p in el.iter("pos") if p.text)
                for form in kebs + rebs:
                    index.setdefault(form, []).append((kebs, rebs, pos))
                if spellings is not None:
                    info = _kanji_info(el)
                    restr = _reading_restrictions(el)
                    uk = any(m.text == "uk" for m in el.iter("misc"))
                    if info or restr or uk:
                        # Entries alike in kebs and rebs share a key: their marks add up
                        old = spellings.get((kebs, rebs), NO_SPELLINGS)
                        spellings[(kebs, rebs)] = (old[0] | info, old[1] | restr, old[2] or uk)
                el.clear()
    parser.close()
    return index


@lru_cache(maxsize=1)
def _data() -> tuple[dict[str, list[Entry]], dict[tuple, Spellings]]:
    data = _load_pickle()
    if data is not None:
        return data
    if not JMDICT_GZ.exists():
        raise FileNotFoundError(f"JMdict not found at {JMDICT_GZ}; resources.ensure() downloads it")
    spellings: dict[tuple, Spellings] = {}
    idx = build(JMDICT_GZ, spellings)
    INDEX_PICKLE.write_bytes(
        pickle.dumps((INDEX_FORMAT, idx, spellings), protocol=pickle.HIGHEST_PROTOCOL)
    )
    return idx, spellings


def index() -> dict[str, list[Entry]]:
    return _data()[0]


def spellings(kebs: tuple[str, ...], rebs: tuple[str, ...]) -> Spellings:
    return _data()[1].get((kebs, rebs), NO_SPELLINGS)


def lookup(form: str) -> list[Entry]:
    return index().get(form, [])


def has_pos(form: str, code: str) -> bool:
    """Whether any entry for form has a POS code equal to or starting with code ("v5" etc.)."""
    return any(code in ps or any(p.startswith(code) for p in ps) for _, _, ps in lookup(form))


def readings(form: str) -> list[str]:
    return [r for _, rs, _ in lookup(form) for r in rs]
