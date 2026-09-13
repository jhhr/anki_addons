"""Names no dictionary knows, found by what surrounds them.

A fictional name or nickname (里樹, ひまりん) is no word of Sudachi's, so it gets cut into
whatever words Sudachi does have (里 + 樹, ひま + りん). Japanese grammar marks names often enough
to find them anyway:

- an honorific right after one: 里樹さま, 阿多さんの;
- a nickname suffix fused to one: ひま|りん, さっ|ちん;
- the name opening quoted speech as the one addressed: 「ひまりん、それ褒めてないでしょ」.

One anchored occurrence names it everywhere, so the lexicon is built over a whole corpus of
sentences and then looked up in each one, bare mentions included (里樹はうつむいた). This is the
document-level approach of character detection in novels, where per-sentence NER models do
badly on invented names.

An honorific follows plenty of words that aren't names, though: kinship and role nouns (娘さん,
王陛下, 八百屋さん) and お-words (お医者さん). So a candidate that is a dictionary word read the way
the dictionary reads it is kept only when its anchored uses are most of its uses: 娘 is anchored
3 times in 45, 梨花 3 in 4.

Morphs are the generator's (surface, pos, start, end); this module doesn't tokenize.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from ..kana_conv import to_hiragana
from . import jmdict_index as jmdict

# Suffixes that almost only ever follow a person. The name stays a word of its own before them.
HONORIFICS = frozenset(
    {
        "さん",
        "さま",
        "様",
        "ちゃん",
        "君",
        "くん",
        "殿",
        "どの",
        "氏",
        "先輩",
        "先生",
        "師匠",
        "陛下",
        "殿下",
        "閣下",
        "卿",
        "妃",
        "姫",
        "はん",
    }
)
# Suffixes that make a nickname out of a name: ひま + りん is the nickname ひまりん.
NICKNAME_SUFFIXES = frozenset({"りん", "たん", "ちん", "っち", "ぽん", "にゃん"})
# Nouns that stand before an honorific without being a name: 侍女たちさま, 皆さん, 自分様.
NOT_NAME = frozenset(
    {
        "侍女",
        "たち",
        "達",
        "ら",
        "等",
        "方",
        "人",
        "者",
        "男",
        "女",
        "子",
        "皆",
        "みんな",
        "自分",
        "相手",
        "本人",
        "今",
        "昨日",
        "明日",
    }
)
# A polite prefix before an honorific makes a polite word, not a name: お医者さん, お祖母ちゃん.
POLITE_PREFIXES = frozenset({"お", "御", "ご"})
MAX_NAME_MORPHS = 4
# A dictionary word candidate needs this many anchors, and at least half its uses anchored
MIN_WORD_ANCHORS = 2
VOCATIVE_RE = re.compile(r"[「『]\s*([^「」『』、。！？\s]{2,8})[、！？…]")
KANA_RE = re.compile(r"[ぁ-ゖァ-ヺー]+")
HIRAGANA_RE = re.compile(r"[ぁ-ゖー]+")

HONORIFIC = "honorific"
NICKNAME = "nickname"
VOCATIVE = "vocative"
SUDACHI = "sudachi"  # Sudachi tags it 固有名詞 too; only ever added to an anchored name

# (written form, reading) -> whether a dictionary has it as a word read so
DictionaryWord = Callable[[str, str], bool]
# (start, end) natural offsets -> the note's reading of that span
Reader = Callable[[int, int], str]


@dataclass
class NameEntry:
    count: int = 0
    sources: set[str] = field(default_factory=set)
    readings: set[str] = field(default_factory=set)  # the note's readings at the anchors


def jmdict_word(form: str, reading: str) -> bool:
    """JMdict has `form` read `reading`; a reading that is no kana (a kanji span with no
    furigana) fits any."""
    hira = to_hiragana(reading)
    unread = not KANA_RE.fullmatch(reading)
    return any(
        unread or hira in {to_hiragana(r) for r in rebs} for _, rebs, _ in jmdict.lookup(form)
    )


def _nounish(m) -> bool:
    """A morph that can be part of a name: a noun but a number, a prefix, a noun-like suffix
    (樹 of 里樹 is one to Sudachi)."""
    if m.pos[0] == "名詞":
        return m.pos[1] != "数詞"
    return m.pos[0] == "接頭辞" or tuple(m.pos[:2]) == ("接尾辞", "名詞的")


def _is_honorific(m) -> bool:
    return m.surface in HONORIFICS and m.pos[0] in ("接尾辞", "名詞")


def _is_proper(m) -> bool:
    return tuple(m.pos[:2]) == ("名詞", "固有名詞")


def _read(read: Optional[Reader], morphs: Sequence) -> str:
    return read(morphs[0].start, morphs[-1].end) if read else "".join(m.surface for m in morphs)


def honorific_spans(morphs: Sequence) -> list[tuple[int, int]]:
    """Morph index ranges [j, i) right before an honorific: up to MAX_NAME_MORPHS adjacent
    name-like morphs."""
    out = []
    for i, m in enumerate(morphs):
        if not _is_honorific(m):
            continue
        j = i
        while (
            j > 0
            and i - j < MAX_NAME_MORPHS
            and morphs[j - 1].end == morphs[j].start
            and _nounish(morphs[j - 1])
            and morphs[j - 1].surface not in NOT_NAME
            and not _is_honorific(morphs[j - 1])
        ):
            j -= 1
        if j < i:
            out.append((j, i))
    return out


def honorific_names(morphs: Sequence) -> list[str]:
    return ["".join(x.surface for x in morphs[j:i]) for j, i in honorific_spans(morphs)]


def name_morphs(
    morphs: Sequence, read: Optional[Reader] = None, word: Optional[DictionaryWord] = None
) -> Sequence:
    """The name in an anchored run of morphs, or nothing. A polite prefix makes it a polite word
    (お医者); with a dictionary, a word of two or more characters inside the run is no part of
    the name, and the last run after one is (当時|ヴィルヘルム, アレン|叔父, もの|梨花)."""
    if morphs[0].pos[0] == "接頭辞" and morphs[0].surface in POLITE_PREFIXES:
        return []
    if word and len(morphs) > 1:
        runs: list[list] = [[]]
        for m in morphs:
            if len(m.surface) > 1 and not _is_proper(m) and word(m.surface, _read(read, [m])):
                runs.append([])
            else:
                runs[-1].append(m)
        morphs = next((r for r in reversed(runs) if r), [])
    if not morphs or morphs[0].pos[0] == "接尾辞" or morphs[-1].pos[0] == "接頭辞":
        return []  # 営業部|長, 大|旦那: only the suffix or the prefix was left
    return morphs


def nickname_spans(morphs: Sequence) -> list[tuple[int, int]]:
    """A noun with a nickname suffix fused to it, as one name: ひま + りん -> ひまりん."""
    return [
        (k, k + 2)
        for k, (prev, m) in enumerate(zip(morphs, morphs[1:]))
        if m.surface in NICKNAME_SUFFIXES
        and m.pos[0] == "接尾辞"
        and prev.end == m.start
        and prev.pos[0] == "名詞"
        and prev.pos[1] != "数詞"
        and prev.surface not in NOT_NAME
    ]


def nickname_names(morphs: Sequence) -> list[str]:
    return ["".join(x.surface for x in morphs[j:i]) for j, i in nickname_spans(morphs)]


def vocative_names(natural: str) -> list[str]:
    """What opens quoted speech before a comma or an exclamation: 「ひまりん、."""
    return VOCATIVE_RE.findall(natural)


def build_lexicon(
    sentences: Iterable[tuple],
    min_nickname: int = 2,
    word: Optional[DictionaryWord] = None,
    rejected: Optional[dict[str, str]] = None,
) -> dict[str, NameEntry]:
    """Names over a corpus of (natural text, morphs) or (natural text, morphs, reader).

    An honorific names on its own; a nickname shape needs `min_nickname` occurrences, a vocative
    one, or (given `word`) to be no dictionary word; a vocative only confirms a name found
    otherwise, since 「先輩、 or 「嘘！ open speech just as well. Given `word`, a name that is a
    dictionary word read as one (娘, 梨花) and Sudachi never tagged 固有名詞 at an anchor must be
    anchored MIN_WORD_ANCHORS times and in at least half its uses; a hiragana one (おじ, だんな)
    never is a name. Why a candidate was dropped goes into `rejected`."""
    corpus = [(s[0], s[1], s[2] if len(s) > 2 else None) for s in sentences]
    honorific: Counter = Counter()
    nickname: Counter = Counter()
    vocative: Counter = Counter()
    readings: dict[str, set] = defaultdict(set)
    proper_anchor: set[str] = set()  # Sudachi tagged every morph of it 固有名詞 at an anchor
    known: set[str] = set()  # read as a dictionary word at an anchor
    sudachi: set[str] = set()
    drop = rejected if rejected is not None else {}

    def anchor(counter: Counter, run: Sequence, read: Optional[Reader]) -> None:
        name = "".join(m.surface for m in run)
        reading = _read(read, run)
        counter[name] += 1
        readings[name].add(reading)
        if all(_is_proper(m) for m in run):
            proper_anchor.add(name)
        elif word and word(name, reading):
            known.add(name)

    for natural, morphs, read in corpus:
        for j, i in honorific_spans(morphs):
            run = name_morphs(morphs[j:i], read, word)
            if run:
                anchor(honorific, run, read)
            else:
                drop.setdefault("".join(m.surface for m in morphs[j:i]), "not a name part")
        for j, i in nickname_spans(morphs):
            anchor(nickname, morphs[j:i], read)
        vocative.update(vocative_names(natural))
        sudachi.update(m.surface for m in morphs if _is_proper(m))
    lexicon: dict[str, NameEntry] = {}

    def add(name: str, n: int, source: str) -> None:
        entry = lexicon.setdefault(name, NameEntry())
        entry.count += n
        entry.sources.add(source)
        entry.readings |= readings[name]

    for name, n in honorific.items():
        add(name, n, HONORIFIC)
    for name, n in nickname.items():
        if n >= min_nickname or name in vocative or (word and name not in known):
            add(name, n, NICKNAME)
    for name, n in vocative.items():
        if name in lexicon:
            add(name, n, VOCATIVE)
    for name in sudachi & lexicon.keys():
        lexicon[name].sources.add(SUDACHI)

    suspects = {n for n in lexicon if n in known and n not in proper_anchor}
    for name in [n for n in suspects if HIRAGANA_RE.fullmatch(n)]:
        del lexicon[name]
        suspects.discard(name)
        drop[name] = "hiragana word"
    uses: Counter = Counter()
    if suspects:
        sub = {n: lexicon[n] for n in suspects}
        for _, morphs, read in corpus:
            uses.update(n for _, _, n in find_names(morphs, sub, read))
    for name in suspects:
        n = lexicon[name].count
        if n < MIN_WORD_ANCHORS or 2 * n < uses[name]:
            del lexicon[name]
            drop[name] = f"word, anchored {n} of {uses[name]}"
    return lexicon


def _reads_as(entry: Optional[NameEntry], name: str, reading: str) -> bool:
    """Whether a mention is read as the name was at its anchors. An unread span (the name's own
    text) fits any: 真[まこと]くん doesn't make 真[しん] a name, but 真 with no furigana may be."""
    if not entry or not entry.readings or reading == name:
        return True
    return name in entry.readings or reading in entry.readings


def find_names(
    morphs: Sequence, lexicon: dict, read: Optional[Reader] = None
) -> list[tuple[int, int, str]]:
    """(start, end, name) of the lexicon's names in a sentence, longest first, each starting
    and ending on a morph boundary: 里樹 is found in 里|樹|は, not in 里|樹木. Given the note's
    `read`ings, a mention read otherwise than at the anchors is none."""
    out = []
    i = 0
    longest = MAX_NAME_MORPHS + 1  # a nickname is a name plus its suffix
    while i < len(morphs):
        for j in range(min(len(morphs), i + longest), i, -1):
            if any(a.end != b.start for a, b in zip(morphs[i : j - 1], morphs[i + 1 : j])):
                continue
            name = "".join(m.surface for m in morphs[i:j])
            if name in lexicon and _reads_as(lexicon[name], name, _read(read, morphs[i:j])):
                out.append((morphs[i].start, morphs[j - 1].end, name))
                i = j
                break
        else:
            i += 1
    return out
