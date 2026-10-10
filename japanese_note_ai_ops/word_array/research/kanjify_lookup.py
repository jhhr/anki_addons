"""Read-only lookups for the kanjify golden set's agents: JMdict's entries with their senses,
Sudachi's reading of a text, a step 1 word's uses, and a search of the dumped sentences.

The agents run headless with no write tool and no other command, so everything they need to
look up offline is here. Japanese on a Windows command line is mangled, so a word may be given
as code points (`U+3088U+308B` for よる). `\\u` escapes work too, but only where nothing
decodes them first: a tool call's JSON turns `\\u3088` back into よる before the command runs,
and Git Bash then fails on it. Plain text works where the shell passes it.

    py -3.10 word_array/research/kanjify_lookup.py jmdict WORD [--all]
    py -3.10 word_array/research/kanjify_lookup.py sudachi TEXT
    py -3.10 word_array/research/kanjify_lookup.py uses WID [--kind kana|kanjified]
    py -3.10 word_array/research/kanjify_lookup.py grep TEXT [--max 40]

`jmdict` lists every entry written or read WORD: its spellings with JMdict's marks (rK rarely
used, oK outdated, sK search-only, ateji, common), its readings, and each sense's part of
speech, notes and glosses, a sense limited to some spellings saying which. The first run builds
the sense index from `user_files/jmdict/JMdict_e.gz` into `jmdict_senses.pkl` beside it (about
a minute). `sudachi` prints each token's reading and normalized form: whether Sudachi reads a
kanji spelling as the word meant (a loanword's, KANJI-16 of the policy). `uses` prints every use of an inventory word; `grep` finds TEXT in the dump's input
sentences and current kanjified fields (furigana and tags dropped for the search).
"""

from __future__ import annotations

import argparse
import codecs
import gzip
import json
import pickle
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from _bootstrap import load

import kanjify_golden as golden

jmdict_index = load("jmdict_index")

SENSES_PICKLE = jmdict_index.DATA_DIR / "jmdict_senses.pkl"
SENSES_FORMAT = 1
ENTITY_RE = re.compile(r"&([\w.-]+);")
ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")
CODE_POINT_RE = re.compile(r"U\+([0-9a-fA-F]{4,5})")
FURI_RE = re.compile(r" ?([^\s\[\]<>]+)\[[^\]]*\]")
TAG_RE = re.compile(r"<[^>]+>")
MAX_GLOSSES = 6


def decode_arg(text: str) -> str:
    """An argument as the agent meant it: code points and `\\u` escapes decoded."""
    text = CODE_POINT_RE.sub(lambda m: chr(int(m[1], 16)), text)
    if ESCAPE_RE.search(text):
        return codecs.decode(text, "unicode_escape")
    return text


def _entry(el: ET.Element) -> dict:
    kebs = []
    for k in el.iter("k_ele"):
        marks = [i.text for i in k.iter("ke_inf") if i.text]
        if k.find("ke_pri") is not None:
            marks.append("common")
        kebs.append((k.findtext("keb"), marks))
    rebs = []
    for r in el.iter("r_ele"):
        marks = [i.text for i in r.iter("re_inf") if i.text]
        if r.find("re_nokanji") is not None:
            marks.append("no kanji")
        restr = [x.text for x in r.iter("re_restr") if x.text]
        if restr:
            marks.append("only " + "/".join(restr))
        rebs.append((r.findtext("reb"), marks))
    senses: list[dict] = []
    pos: list[str] = []
    for s in el.iter("sense"):
        this_pos = [p.text for p in s.iter("pos") if p.text]
        pos = this_pos or pos  # JMdict: a sense without pos has the previous sense's
        notes = [m.text for tag in ("misc", "field", "dial") for m in s.iter(tag) if m.text]
        notes += [i.text for i in s.iter("s_inf") if i.text]
        only = [x.text for tag in ("stagk", "stagr") for x in s.iter(tag) if x.text]
        glosses = [g.text for g in s.iter("gloss") if g.text][:MAX_GLOSSES]
        senses.append({"pos": pos, "notes": notes, "only": only, "glosses": glosses})
    return {"seq": el.findtext("ent_seq"), "kebs": kebs, "rebs": rebs, "senses": senses}


def build_senses(gz: Path) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=("end",))
    in_body = False
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        for line in f:
            if not in_body:
                start = line.find("<JMdict>")
                if start < 0:
                    continue
                line, in_body = line[start:], True
            parser.feed(ENTITY_RE.sub(r"\1", line))
            # typeshed's read_events type unpacks to one value under this mypy
            events: Any = parser.read_events()
            for _, el in events:
                if not isinstance(el, ET.Element) or el.tag != "entry":
                    continue
                entry = _entry(el)
                for form in {k for k, _ in entry["kebs"]} | {r for r, _ in entry["rebs"]}:
                    index.setdefault(form, []).append(entry)
                el.clear()
    return index


_senses: Optional[dict[str, list[dict]]] = None


def senses_index() -> dict[str, list[dict]]:
    global _senses
    if _senses is None:
        try:
            fmt, data = pickle.loads(SENSES_PICKLE.read_bytes())
            _senses = data if fmt == SENSES_FORMAT else None
        except (OSError, ValueError, TypeError, pickle.UnpicklingError, EOFError):
            _senses = None
        if _senses is None:
            print("building the JMdict sense index (once)...", file=sys.stderr)
            _senses = build_senses(jmdict_index.JMDICT_GZ)
            SENSES_PICKLE.write_bytes(
                pickle.dumps((SENSES_FORMAT, _senses), protocol=pickle.HIGHEST_PROTOCOL)
            )
    return _senses


def entry_text(entry: dict) -> str:
    def forms(items):
        return ", ".join(f + (f" ({', '.join(m)})" if m else "") for f, m in items) or "-"

    lines = [f"JMdict {entry['seq']}: spellings {forms(entry['kebs'])}; readings {forms(entry['rebs'])}"]
    for n, s in enumerate(entry["senses"], 1):
        head = f"  {n}. [{', '.join(s['pos'])}]"
        if s["only"]:
            head += f" (only {'/'.join(s['only'])})"
        if s["notes"]:
            head += f" ({'; '.join(s['notes'])})"
        lines.append(f"{head} {'; '.join(s['glosses'])}")
    return "\n".join(lines)


def jmdict_text(word: str, with_kana_only: bool = False) -> str:
    """Every JMdict entry written or read `word`, the ones with a kanji spelling first; an entry
    with no kanji spelling only with `with_kana_only`, or when it is all there is."""
    entries = senses_index().get(word, [])
    with_kanji = [e for e in entries if e["kebs"]]
    shown = entries if with_kana_only or not with_kanji else with_kanji
    if not shown:
        return f"JMdict has no entry written or read {word}."
    out = [entry_text(e) for e in shown]
    hidden = len(entries) - len(shown)
    if hidden:
        out.append(f"({hidden} entries without a kanji spelling left out)")
    return "\n".join(out)


def read_words() -> dict[str, dict]:
    return {w["wid"]: w for w in golden.read_jsonl(golden.INVENTORY / "words.jsonl")}


def plain(text: str) -> str:
    """Field text as it reads with kanji: tags dropped, furigana dropped."""
    return FURI_RE.sub(lambda m: m[1], TAG_RE.sub("", text)).replace(" ", "")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("jmdict")
    p.add_argument("word")
    p.add_argument("--all", action="store_true", help="also entries without kanji")
    p = sub.add_parser("sudachi")
    p.add_argument("text")
    p = sub.add_parser("uses")
    p.add_argument("wid")
    p.add_argument("--kind", choices=("kana", "kanjified"))
    p = sub.add_parser("grep")
    p.add_argument("text")
    p.add_argument("--max", type=int, default=40)
    args = parser.parse_args()

    if args.command == "jmdict":
        print(jmdict_text(decode_arg(args.word), args.all))
    elif args.command == "sudachi":
        generator = load("generator")
        for m in generator.tokenize(decode_arg(args.text)):
            print(f"{m.surface}\treading {m.reading}\tnormalized {m.norm}\t{','.join(m.pos[:2])}")
    elif args.command == "uses":
        word = read_words().get(args.wid)
        if word is None:
            print(f"no word {args.wid} in the inventory")
            return 1
        uses = [u for u in word["uses"] if not args.kind or u["kind"] == args.kind]
        print(f"{word['word']} ({word['kana']}, {word['pos']}): {len(uses)} uses")
        for u in uses:
            kanji = f" {u['kanji']}" if u["kanji"] else ""
            print(f"{u['sid']} {u['kind']}{kanji}: {u['text']}")
    else:
        text = decode_arg(args.text)
        seen = 0
        for row in golden.read_jsonl(golden.INVENTORY / "sentences.jsonl"):
            if text in plain(row["sentence"]) or text in plain(row["current"]):
                print(f"{row['sid']}: {plain(row['current'])}")
                seen += 1
                if seen >= args.max:
                    print(f"(stopped at {args.max})")
                    break
        if not seen:
            print(f"no sentence holds {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
