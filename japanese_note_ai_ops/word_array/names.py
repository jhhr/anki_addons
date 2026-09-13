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

Morphs are the generator's (surface, pos, start, end); this module doesn't tokenize.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

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
MAX_NAME_MORPHS = 4
VOCATIVE_RE = re.compile(r"[「『]\s*([^「」『』、。！？\s]{2,8})[、！？…]")

HONORIFIC = "honorific"
NICKNAME = "nickname"
VOCATIVE = "vocative"
SUDACHI = "sudachi"  # Sudachi tags it 固有名詞 too; only ever added to an anchored name


@dataclass
class NameEntry:
    count: int = 0
    sources: set[str] = field(default_factory=set)


def _nounish(m) -> bool:
    """A morph that can be part of a name: a noun but a number, a prefix, a noun-like suffix
    (樹 of 里樹 is one to Sudachi)."""
    if m.pos[0] == "名詞":
        return m.pos[1] != "数詞"
    return m.pos[0] == "接頭辞" or tuple(m.pos[:2]) == ("接尾辞", "名詞的")


def _is_honorific(m) -> bool:
    return m.surface in HONORIFICS and m.pos[0] in ("接尾辞", "名詞")


def honorific_names(morphs: Sequence) -> list[str]:
    """The names right before an honorific: up to MAX_NAME_MORPHS adjacent name-like morphs."""
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
            out.append("".join(x.surface for x in morphs[j:i]))
    return out


def nickname_names(morphs: Sequence) -> list[str]:
    """A noun with a nickname suffix fused to it, as one name: ひま + りん -> ひまりん."""
    return [
        prev.surface + m.surface
        for prev, m in zip(morphs, morphs[1:])
        if m.surface in NICKNAME_SUFFIXES
        and m.pos[0] == "接尾辞"
        and prev.end == m.start
        and prev.pos[0] == "名詞"
        and prev.pos[1] != "数詞"
        and prev.surface not in NOT_NAME
    ]


def vocative_names(natural: str) -> list[str]:
    """What opens quoted speech before a comma or an exclamation: 「ひまりん、."""
    return VOCATIVE_RE.findall(natural)


def build_lexicon(
    sentences: Iterable[tuple[str, Sequence]], min_nickname: int = 2
) -> dict[str, NameEntry]:
    """Names over a corpus of (natural text, morphs). An honorific names on its own; a nickname
    shape needs `min_nickname` occurrences or a vocative one; a vocative only confirms a name
    found otherwise, since 「先輩、 or 「嘘！ open speech just as well."""
    honorific: Counter = Counter()
    nickname: Counter = Counter()
    vocative: Counter = Counter()
    sudachi: set[str] = set()
    for natural, morphs in sentences:
        honorific.update(honorific_names(morphs))
        nickname.update(nickname_names(morphs))
        vocative.update(vocative_names(natural))
        sudachi.update(m.surface for m in morphs if tuple(m.pos[:2]) == ("名詞", "固有名詞"))
    lexicon: dict[str, NameEntry] = {}

    def add(name: str, n: int, source: str) -> None:
        entry = lexicon.setdefault(name, NameEntry())
        entry.count += n
        entry.sources.add(source)

    for name, n in honorific.items():
        add(name, n, HONORIFIC)
    for name, n in nickname.items():
        if n >= min_nickname or name in vocative:
            add(name, n, NICKNAME)
    for name, n in vocative.items():
        if name in lexicon:
            add(name, n, VOCATIVE)
    for name in sudachi & lexicon.keys():
        lexicon[name].sources.add(SUDACHI)
    return lexicon


def find_names(morphs: Sequence, lexicon: dict) -> list[tuple[int, int, str]]:
    """(start, end, name) of the lexicon's names in a sentence, longest first, each starting
    and ending on a morph boundary: 里樹 is found in 里|樹|は, not in 里|樹木."""
    out = []
    i = 0
    longest = MAX_NAME_MORPHS + 1  # a nickname is a name plus its suffix
    while i < len(morphs):
        for j in range(min(len(morphs), i + longest), i, -1):
            if any(a.end != b.start for a, b in zip(morphs[i : j - 1], morphs[i + 1 : j])):
                continue
            name = "".join(m.surface for m in morphs[i:j])
            if name in lexicon:
                out.append((morphs[i].start, morphs[j - 1].end, name))
                i = j
                break
        else:
            i += 1
    return out
