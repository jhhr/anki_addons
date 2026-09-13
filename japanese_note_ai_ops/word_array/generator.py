"""Word array generator: Sudachi + rule-based grouping + JMdict multi-word units.

Output elements are [raw_text, part_of_speech, dict_form, reading, match_data, sub_words];
tags, punctuation and other non-word text are single-element arrays. Concatenating the top-level
raw_text values gives back the sentence without <b> tags.

Which words exist is decided by concrete rules only; whether a word is worth matching to a note
is left to the word matching judge, so the rules err towards more parents and more sub-words.

Stages
  1. text_map: strip tags and furigana, revert <k> words to kana -> natural text
  2. Sudachi, SplitMode.C, with the SplitMode.A split of each long unit kept for sub-words
  3. group morphemes into words (a verb/adjective plus its inflection chain)
  4. merge words whose boundary would cut a furigana group (八紘|一宇 -> 八紘一宇)
  5. multi-word candidates: JMdict n-grams (last word also deinflected)
  6. structure: every JMdict match that is a word of the text becomes a parent, nested when
     one contains another; words the tokenizer has as one unit get sub-words from JMdict.
     Sub-words that end inside a furigana group get their share of its reading, and are
     dropped where it can't be shared out (jukujikun can't be split inside a word)
  7. dictionary form, reading and part of speech per word; readings come from the note's own
     furigana wherever it has them
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Optional

from sudachipy import Dictionary, SplitMode

from ..kana_conv import to_hiragana
from . import jmdict_index as jmdict
from . import match_flags, numbers, resources, text_map
from .text_map import TextMap

KANJI_RE = re.compile(r"[一-龯㐀-䶿々]")
# What a reading can't hold: kanji or digits the furigana left unread
UNREAD_RE = re.compile(r"[一-龯㐀-䶿々0-9０-９]")


@lru_cache(maxsize=1)
def _tokenizer():
    dictionary = resources.sudachi_dictionary()
    if dictionary is None:
        raise resources.ResourcesMissing(
            "No Sudachi dictionary; resources.ensure() downloads one"
            " (from a script: word_array/research/setup_resources.py)"
        )
    return Dictionary(dict=dictionary).create()


@dataclass
class Morph:
    start: int  # natural text offsets
    end: int
    surface: str
    pos: tuple[str, ...]
    lemma: str  # dictionary_form, natural spelling
    norm: str  # normalized_form, Sudachi's kanji spelling (する -> 為る, これ -> 此れ)
    reading: str  # hiragana reading of the surface
    subs: list[Morph] = field(default_factory=list)  # SplitMode.A split, compounds only


@dataclass
class Word:
    morphs: list[Morph]
    kind: str = "word"  # word | punct | merged | expression
    subs: list[Word] = field(default_factory=list)
    jm_pos: frozenset[str] = frozenset()
    jm_form: str = ""  # dictionary form known up front: a JMdict match, or a furigana merge
    jm_readings: tuple[str, ...] = ()
    # expression only: JMdict has it as written here, rather than with the last word deinflected
    surface_match: bool = False
    followed_by_verb: bool = False
    followed_by_suru: bool = False
    nested: bool = False  # a sub-word of another word, set when emitting

    @property
    def start(self) -> int:
        return self.morphs[0].start

    @property
    def end(self) -> int:
        return self.morphs[-1].end

    @property
    def head(self) -> Morph:
        return self.morphs[0]

    @property
    def natural(self) -> str:
        return "".join(m.surface for m in self.morphs)


@dataclass
class Candidate:
    i: int  # word index range [i, j)
    j: int
    form: str
    readings: tuple[str, ...]
    pos: frozenset[str]
    surface: bool  # matched as written, not with the last word deinflected
    written: str  # the text's own spelling of what was looked up
    spellings: tuple[str, ...]  # the kanji spellings of the JMdict entries matched


@dataclass
class Analysis:
    text_map: TextMap
    words: list[Word]  # after grouping and furigana merges, before candidates
    candidates: list[Candidate]
    final: list[Word]
    array: list[list]


# --- 2. tokenizing -------------------------------------------------------------------------


def _morph(m) -> Morph:
    return Morph(
        start=m.begin(),
        end=m.end(),
        surface=m.surface(),
        pos=tuple(m.part_of_speech()),
        lemma=m.dictionary_form(),
        norm=m.normalized_form(),
        reading=to_hiragana(m.reading_form()),
    )


def tokenize(natural: str) -> list[Morph]:
    out = []
    for m in _tokenizer().tokenize(natural, SplitMode.C):
        if m.end() <= m.begin():
            # Sudachi normalizes its input before tokenizing (… -> ...) and maps the result
            # back to the text it was given, so a character that expanded comes back as one
            # morpheme covering it plus empty ones with no text to point at - the last of which
            # start past the end of `natural`.
            continue
        mo = _morph(m)
        subs = [s for s in m.split(SplitMode.A) if s.end() > s.begin()]
        if len(subs) > 1:
            mo.subs = [_morph(s) for s in subs]
        out.append(mo)
    return _split_number_counters(out)


def _split_number_counters(morphs: list[Morph]) -> list[Morph]:
    """'1日' comes out as one token read ついたち ('１日' as an adverb); the furigana marks 日
    as its own word."""
    out = []
    for m in morphs:
        mm = re.fullmatch(r"(\d+)(\D+)", m.surface)
        if mm:
            n = len(mm.group(1))
            num, rest = mm.group(1), mm.group(2)
            out.append(Morph(m.start, m.start + n, num, ("名詞", "数詞"), num, num, num))
            out.append(
                Morph(m.start + n, m.end, rest, ("接尾辞", "名詞的", "助数詞"), rest, rest, "")
            )
        else:
            out.append(m)
    return out


# --- 3. grouping ---------------------------------------------------------------------------

INFLECTING = ("動詞", "形容詞")
ATTACH_CONJ_PARTICLES = {"て", "で", "ば"}
ATTACH_AUX_VERBS = {"いる", "居る"}  # しまう, やる, おく... stay separate words (gold ex. 19)
# Modal auxiliaries that leave the verb's form alone are words of their own: 捌いとる + らしい
SEPARATE_AUX = {"らしい", "べし", "まい"}
# Copula forms listed as particles (開豁に, 自由自在な, 気でいる)
PARTICLE_COPULA = ("な", "に", "で")
FUNCTION_POS = ("助詞", "助動詞")


def is_copula(m: Morph) -> bool:
    return m.pos[0] == "助動詞" and m.norm in ("だ", "です")


def _attaches(prev: Morph, nxt: Morph, head: Morph) -> bool:
    if head.pos[0] not in INFLECTING and head.pos[0] != "助動詞":
        return False
    if nxt.pos[0] == "助動詞":
        if nxt.lemma in SEPARATE_AUX:
            return False
        # だ as past tense after onbin (沈んだ) attaches; a copula after a noun starts a word
        return not (
            is_copula(nxt)
            and head.pos[0] not in INFLECTING
            and prev is head
            and not is_copula(head)
        )
    if nxt.pos[:2] == ("助詞", "接続助詞") and nxt.surface in ATTACH_CONJ_PARTICLES:
        return head.pos[0] in INFLECTING
    if (
        nxt.pos[0] == "動詞"
        and nxt.lemma in ATTACH_AUX_VERBS
        and prev.surface in ("て", "で")
        and head.pos[0] in INFLECTING
    ):
        return True
    return is_copula(head) and nxt.pos[0] == "形容詞" and nxt.lemma == "ない"  # じゃない


def group(morphs: list[Morph]) -> list[list[Morph]]:
    groups: list[list[Morph]] = []
    for m in morphs:
        if groups and _attaches(groups[-1][-1], m, groups[-1][0]):
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups


def _compound_subs(g: list[Morph]) -> list[Word]:
    """Sub-words of a Sudachi long unit; the inflection after it goes on the last sub-word."""
    parts = group(g[0].subs)
    if len(parts) < 2:
        return []
    parts[-1] = parts[-1] + g[1:]
    return [Word(p) for p in parts]


def to_words(morphs: list[Morph]) -> list[Word]:
    """A Sudachi long unit is one word, and its short units, when it has several, are its
    sub-words: 飛行機 -> 飛行 + 機, 私達 -> 私 + 達, 遂行能力 -> 遂行 + 能力."""
    words: list[Word] = []
    for g in group(morphs):
        kind = "punct" if g[0].pos[0] in ("補助記号", "空白") else "word"
        words.append(Word(g, kind=kind, subs=_compound_subs(g) if g[0].subs else []))
    return words


# --- 4. furigana-cut merge -----------------------------------------------------------------


def merge_cut_groups(tm: TextMap, words: list[Word]) -> list[Word]:
    """A furigana group is never split between top-level words.

    When the merged text is a JMdict word read as the furigana says (業|者, 八紘|一宇), the
    tokenizer split a word: the merge is one word, whose sub-words are decided in step 6 like
    any other's. Otherwise (天|高く, 軽音|部, 馬|肥 which JMdict has as うまごやし) the pieces are
    the words and the merge only holds them together - unless every piece is a lone kanji
    read in on'yomi, a name cut into its characters (里|樹), which has no pieces to keep.
    """
    out: list[Word] = []
    for w in words:
        if not out or tm.boundary_ok(w.start):
            out.append(w)
            continue
        prev = out.pop()
        pieces = (prev.subs if prev.kind == "merged" else [prev]) + [w]
        morphs = prev.morphs + w.morphs
        start, end = morphs[0].start, morphs[-1].end
        written = tm.written_form(start, end)
        hits = jmdict.lookup(written)
        reading = to_hiragana(tm.surface_reading(start, end))
        if any(reading == to_hiragana(r) for _, rs, _ in hits for r in rs):
            out.append(
                Word(
                    morphs,
                    jm_form=written,
                    jm_readings=tuple(r for _, rs, _ in hits for r in rs),
                    jm_pos=frozenset(p for _, _, ps in hits for p in ps),
                )
            )
        else:
            keep = not all(tm.is_onyomi_kanji(p.start, p.end) for p in pieces)
            out.append(Word(morphs, kind="merged", subs=pieces if keep else [], jm_form=written))
    return out


# --- 5. candidates -------------------------------------------------------------------------


def _forms(tm: TextMap, ws: list[Word]) -> list[tuple[str, str, bool]]:
    """(form, the text's own spelling of it, as written) to look up: the text as written and
    as tokenized, then with the last word deinflected (と言った -> と言う)."""
    written = [tm.written_form(w.start, w.end) for w in ws]
    natural = [w.natural for w in ws]
    as_written = "".join(written)
    forms = [(as_written, as_written, True), ("".join(natural), as_written, True)]
    # Numbers as numerals, however the text writes them: 1日 and １日 are both 一日
    numeral = "".join(_surface_form(tm, w) for w in ws)
    if numeral != as_written:
        forms.append((numeral, numeral, True))
    last = ws[-1]
    if last.head.pos[0] in INFLECTING:
        # As a sub-word, so a verb stem ending the match deinflects: 気に入り -> 気に入る
        deinflected = "".join(written[:-1]) + dict_form(tm, replace(last, nested=True))
        forms += [
            (deinflected, deinflected, False),
            ("".join(natural[:-1]) + last.head.lemma, deinflected, False),
        ]
    unique: dict[str, tuple[str, str, bool]] = {}
    for form in forms:
        unique.setdefault(form[0], form)
    return list(unique.values())


def reads_as(parts: list[str], readings: tuple[str, ...]) -> bool:
    """Whether words the note reads as `parts` can be an entry read as one of `readings`, a
    word after the first allowed rendaku the furigana has no group to show: 慈悲[じひ] 深[ふか]い
    is じひぶかい."""
    known = {to_hiragana(r) for r in readings}
    options = [[parts[0]]] + [dict.fromkeys([p, voiced(p)]) for p in parts[1:]]
    return any("".join(combo) in known for combo in itertools.product(*options))


def jmdict_candidates(tm: TextMap, words: list[Word], max_len: int = 8) -> list[Candidate]:
    cands = []
    for i in range(len(words)):
        if words[i].kind == "punct":
            continue
        for j in range(i + 2, min(len(words), i + max_len) + 1):
            if words[j - 1].kind == "punct":
                break
            parts = [to_hiragana(tm.surface_reading(w.start, w.end)) for w in words[i:j]]
            for form, written, surface in _forms(tm, words[i:j]):
                hits = jmdict.lookup(form)
                if surface and not UNREAD_RE.search("".join(parts)):
                    # A homograph the note's furigana reads otherwise is not this entry:
                    # 彼[かれ]の is no 彼の (あの), 今日[きょう]は no 今日は (こんにちは)
                    hits = [h for h in hits if reads_as(parts, h[1])]
                if hits:
                    cands.append(
                        Candidate(
                            i,
                            j,
                            form,
                            readings=tuple(r for _, rs, _ in hits for r in rs),
                            pos=frozenset(p for _, _, ps in hits for p in ps),
                            surface=surface,
                            written=written,
                            spellings=tuple(k for ks, _, _ in hits for k in ks),
                        )
                    )
                    break
    return cands + _stem_suffix_candidates(tm, words, cands)


def _stem_suffix_candidates(
    tm: TextMap, words: list[Word], cands: list[Candidate]
) -> list[Candidate]:
    """An adjective stem and the na-adjective suffix after it (儚 + げ) as one na-adjective where
    JMdict lacks it, like 寂しげ which JMdict has: not 儚い + a げ of its own, nor 忌々し + げに."""
    out = []
    for i, (a, b) in enumerate(zip(words, words[1:])):
        if (
            len(a.morphs) == 1
            and a.head.pos[0] == "形容詞"
            and a.head.pos[5].startswith("語幹")
            and len(b.morphs) == 1
            and b.head.pos[:2] == ("接尾辞", "形状詞的")
            and not any((c.i, c.j) == (i, i + 2) for c in cands)
        ):
            written = tm.written_form(a.start, b.end)
            out.append(Candidate(i, i + 2, written, (), frozenset({"adj-na"}), True, written, ()))
    return out


# --- 6. structure --------------------------------------------------------------------------

SCRIPT_CHUNK_RE = re.compile(r"[一-龯㐀-䶿々]+|[^一-龯㐀-䶿々]+")


def spelled_alike(written: str, spelling: str) -> bool:
    """Whether a JMdict spelling can be how the text writes a word: the text's kana in the
    same order, and between them the same kanji or kana the text has kanjified. だけの事は有る
    is だけの事はある; を持って is not を以って, and は幾つ is not 背屈. A kanji run of the text
    also agrees with kana around kanji it has: 如何為て is 如何して, 為易い is し易い."""

    def agree(kanji: str, other: str) -> bool:
        return other == kanji or bool(
            kanji and other and set(KANJI_RE.findall(other)) <= set(KANJI_RE.findall(kanji))
        )

    pos, kanji = 0, ""
    for chunk in SCRIPT_CHUNK_RE.findall(written):
        if KANJI_RE.match(chunk):
            kanji = chunk
            continue
        found = spelling.find(chunk, pos)
        if found < 0 or not agree(kanji, spelling[pos:found]):
            return False
        pos, kanji = found + len(chunk), ""
    return agree(kanji, spelling[pos:])


def is_word_match(c: Candidate, words: list[Word]) -> bool:
    """Whether a JMdict match is a word of this text at all - not whether it is worth
    studying, which the word matching judge decides."""
    ws = words[c.i : c.j]
    if all(w.head.pos[0] in FUNCTION_POS for w in ws):
        return False  # function words only: には, のだ, か+の read as 彼の
    if not KANJI_RE.search(c.form):
        # Found only by its kana: a homophone unless JMdict spells the entry the way the text
        # does. The text has often kanjified what JMdict keeps in kana (有る), so only its kana
        # are compared.
        alike = any(spelled_alike(c.written, s) for s in c.spellings)
        if ws[0].head.pos[0] == "助詞":
            # Reaching across a particle, even an entry JMdict has only in kana: は+いくつ
            return KANJI_RE.search(c.written) is not None and alike
        if KANJI_RE.search(c.written) and c.spellings and not alike:
            return False  # 成[な]ると read as 鳴門, 事[こと]に as 殊に
    return True


def _crosses(a: Candidate, b: Candidate) -> bool:
    return a.i < b.i < a.j < b.j or b.i < a.i < b.j < a.j


def choose_matches(cands: list[Candidate], words: list[Word]) -> list[Candidate]:
    """Every JMdict match that is a word of the text becomes a parent. One inside another
    nests in it (様に in 様に成る); of two that cross, the longer wins, then the one found by
    its kanji spelling, then the earlier (一つ over つの)."""
    chosen: list[Candidate] = []
    for c in sorted(cands, key=lambda c: (c.i - c.j, not KANJI_RE.search(c.form), c.i)):
        if not is_word_match(c, words):
            continue
        if any(_crosses(c, d) or (c.i, c.j) == (d.i, d.j) for d in chosen):
            continue
        chosen.append(c)
    return chosen


def nest(words: list[Word], chosen: list[Candidate], lo: int = 0, hi: int = -1) -> list[Word]:
    """The words in [lo, hi) with the chosen matches among them as parents, recursively."""
    hi = len(words) if hi < 0 else hi
    inside = sorted((c for c in chosen if lo <= c.i and c.j <= hi), key=lambda c: (c.i, c.i - c.j))
    out: list[Word] = []
    k = lo
    while k < hi:
        top = next((c for c in inside if c.i == k), None)
        if top is None:
            out.append(words[k])
            k += 1
            continue
        inner = [c for c in inside if c is not top and top.i <= c.i and c.j <= top.j]
        parts = nest(words, inner, top.i, top.j)
        # A furigana merge that is no word of its own (天|高く) lists its pieces
        subs = [s for p in parts for s in (p.subs if p.kind == "merged" and p.subs else [p])]
        out.append(
            Word(
                [m for p in parts for m in p.morphs],
                kind="expression",
                subs=subs,
                jm_pos=top.pos,
                jm_form=top.form,
                jm_readings=top.readings,
                surface_match=top.surface,
            )
        )
        k = top.j
    return out


# JMdict POS -> the Sudachi POS a decomposed piece is labelled with, in order of preference for
# the first piece and for the second (大 as a prefix, 屋 as a suffix)
PIECE_POS = {
    "pref": ("接頭辞",),
    "suf": ("接尾辞",),
    "n-suf": ("接尾辞",),
    "ctr": ("接尾辞",),
    "prt": ("助詞",),
    "pn": ("代名詞",),
    "adj-na": ("形状詞",),
    "adv": ("副詞",),
    "n": ("名詞", "普通名詞"),
}
FIRST_PIECE_POS = ("pref", "pn", "n", "adj-na", "adv", "prt")
SECOND_PIECE_POS = ("prt", "suf", "n-suf", "ctr", "n", "pn", "adj-na", "adv")
NOT_DECOMPOSED_POS = INFLECTING + FUNCTION_POS + ("補助記号", "空白")


def unvoiced(kana: str) -> str:
    """Undo rendaku on the first kana: ぞら -> そら, ごなし -> こなし."""
    return unicodedata.normalize("NFD", kana[:1])[:1] + kana[1:] if kana else kana


def voiced(kana: str) -> str:
    """Apply rendaku to the first kana: かい -> がい. Unchanged where there is no voiced form."""
    first = unicodedata.normalize("NFC", kana[:1] + "゙")
    return (first if len(first) == 1 else kana[:1]) + kana[1:]


def _piece(tm: TextMap, start: int, end: int, second: bool) -> Optional[Word]:
    """Natural span [start, end) as a sub-word, if JMdict has it with the reading the note
    gives it (a second piece may be voiced by rendaku) and it isn't a lone on'yomi kanji."""
    if tm.is_onyomi_kanji(start, end):
        return None
    written = tm.written_form(start, end)
    reading = to_hiragana(tm.surface_reading(start, end))
    if KANJI_RE.search(reading):
        return None  # no reading of its own in the furigana to check against
    readings = {reading, unvoiced(reading)} if second else {reading}
    forms = [written, unvoiced(written)] if second and not KANJI_RE.search(written) else [written]
    codes = {
        p
        for form in dict.fromkeys(forms)
        for _, rs, ps in jmdict.lookup(form)
        if readings & {to_hiragana(r) for r in rs}
        for p in ps
    }
    code = next((c for c in (SECOND_PIECE_POS if second else FIRST_PIECE_POS) if c in codes), None)
    if code is None:
        return None
    natural = tm.natural[start:end]
    return Word([Morph(start, end, natural, PIECE_POS[code], natural, written, reading)])


def decompose(tm: TextMap, w: Word) -> list[Word]:
    """Two JMdict words making up a word the tokenizer has as one unit: 耳元 -> 耳 + 元,
    頭ごなし -> 頭 + ごなし, 正に -> 正 + に. The split falls where the furigana's reading
    splits per kanji, and each piece must pass _piece. Lone on'yomi kanji are refused because
    they are mostly bound morphemes: allowing them splits every on'yomi compound (最|近, 言|語)."""
    if w.kind != "word" or w.subs or (len(w.morphs) > 1 and not w.jm_form):
        return []
    if w.head.pos[0] in NOT_DECOMPOSED_POS or w.head.pos[:2] in (
        ("名詞", "固有名詞"),
        ("名詞", "数詞"),
    ):
        return []
    if not KANJI_RE.search(tm.written_form(w.start, w.end)) or adjective_of(tm, w):
        return []  # an adjective form is no compound: not 大き + な
    for split in range(w.start + 1, w.end):
        if not tm.can_split(split) or _okurigana_tail(tm, w, split):
            continue
        first = _piece(tm, w.start, split, second=False)
        second = first and _piece(tm, split, w.end, second=True)
        if first and second:
            return [first, second]
    return []


def _okurigana_tail(tm: TextMap, w: Word, split: int) -> bool:
    """Whether the kana after `split` are the word's okurigana, not a word: JMdict has らか, か
    and た as words, but 柔らか, 静か and 新た (形状詞) are no 柔 + らか; nor are the adverbs 悉く,
    幾ら, 何しろ. An adverb's particle is a word of its own (正|に, 初め|て). A noun's lone kana
    other than も is okurigana (窪み, 夕べ, 逆さ; 何時|も), and so is a longer tail of a noun
    that is a verb's ます-stem (味わい, 計らい, 見かけ); 赤|ちゃん, 口|コミ, 目|つき are words.
    A conjunction's or interjection's tail is always okurigana (但し, 更に, 並びに; 済みません,
    初めまして), a pronoun's lone kana but に, も and か too (其こ, 其んで; 何|に, 私|たち)."""
    tail = tm.written_form(split, w.end)
    if KANJI_RE.search(tail):
        return False
    if w.head.pos[0] in ("形状詞", "接続詞", "感動詞"):
        return True
    if w.head.pos[0] == "代名詞":
        return len(tail) == 1 and tail not in "にもか"
    if w.head.pos[0] == "名詞":
        written = tm.written_form(w.start, w.end)
        stem = VERB_STEM_TO_DICT.get(written[-1])
        return (
            (len(tail) == 1 and tail != "も")
            or bool(stem and jmdict.has_pos(written[:-1] + stem, "v5"))
            or jmdict.has_pos(written + "る", "v1")
        )
    return w.head.pos[0] == "副詞" and "prt" not in {
        p for _, rs, ps in jmdict.lookup(tail) if tail in rs for p in ps
    }


def add_decompositions(tm: TextMap, words: list[Word]) -> None:
    for w in words:
        if w.subs:
            add_decompositions(tm, w.subs)
        else:
            w.subs = decompose(tm, w)


# --- 6b. furigana for sub-words inside one group -------------------------------------------


def split_group_readings(tm: TextMap, words: list[Word]) -> None:
    """Give every sub-word that ends inside a furigana group its own share of the reading, so
    that no sub-word ever comes out as bare kanji.

    Where the group's reading splits per kanji, text_map does it. A jukujikun group doesn't
    split that way, but it can still be split between two words when their own readings add up
    to it: 為替相場[かわせそうば] -> 為替[かわせ] + 相場[そうば]. When they don't add up, the
    word keeps no sub-words - jukujikun can't be split inside a word.
    """
    for w in words:
        if not w.subs:
            continue
        if _assign_cut_readings(tm, w.subs) and _unread_subs_add_up(tm, w):
            split_group_readings(tm, w.subs)
        else:
            w.subs = []


def _unread_subs_add_up(tm: TextMap, w: Word) -> bool:
    """Where the note gives a word no reading, Sudachi's reading of it has to be shared out
    between its sub-words too. Sudachi's short units don't always add up to the long unit's:
    無人島 splits as 無人[むじん] + 島[むじんとう], and 一日中[いちにちじゅう] as 一 + 日中[にっちゅう].
    A sub-word read nowhere in the furigana may take its own Sudachi or JMdict reading instead
    (島 -> とう); when nothing adds up, the word keeps no sub-words."""
    if not KANJI_RE.search(to_hiragana(tm.surface_reading(w.start, w.end))):
        return True  # the furigana reads it all, and its groups were shared out above
    if w.kind == "expression" and not w.surface_match:
        return True  # its dictionary form and reading are built from its words
    options = []
    unread = []  # sub-words whose reading is Sudachi's alone, and so may be replaced
    for i, s in enumerate(w.subs):
        readings = [_furigana_reading(tm, s.start, s.end, s.morphs)]
        own = len(s.morphs) == 1 and KANJI_RE.search(tm.surface_reading(s.start, s.end))
        if number_value(tm, s) is not None:
            readings.append(dict_reading(tm, s))  # Sudachi reads ３ as ３
        if own:
            written = tm.written_form(s.start, s.end)
            readings += [_sudachi_reading(written)] + list(jmdict.readings(written))
        readings = [to_hiragana(r) for r in readings]
        if i:
            readings += [voiced(r) for r in readings]
        options.append(list(dict.fromkeys(readings)))
        unread.append(bool(own))
    targets = ["".join(m.reading for m in w.morphs)]
    known = [to_hiragana(r) for r in w.jm_readings]
    counted = any(
        number_value(tm, a) is not None
        and (b.head.pos[0] == "接尾辞" or "助数詞可能" in b.head.pos)
        for a, b in zip(w.subs, w.subs[1:])
    )
    if known and targets[0] not in known and not (counted and _has_unread_number(tm, w)):
        # A JMdict match is read as JMdict reads it, which Sudachi's pieces needn't add up to.
        # Not a number before a counter, whose sound changes dict_reading takes from JMdict
        # anyway: 一[いち] + 本[ほん] is いっぽん, while 一 + 日中 is no 一日中
        targets = known
    for combo in itertools.product(*options):
        if "".join(combo) in targets:
            for s, reading, replace in zip(w.subs, combo, unread):
                if replace:
                    s.morphs[0].reading = reading
            return True
    # Furigana on part of the word is mostly the whole reading put on one kanji of it
    # (<b> 無人</b>島[むじんとう]): its sub-words stay, as they may already be linked to notes
    return tm.surface_reading(w.start, w.end) != tm.written_form(w.start, w.end)


def _assign_cut_readings(tm: TextMap, subs: list[Word]) -> bool:
    """Work out the reading of each piece of every group these sub-words cut where it doesn't
    split per kanji, and record it on the text map."""
    cuts: dict[int, list[int]] = {}
    for s in subs[1:]:
        si, off = tm.nat_pos[s.start]
        if off and not tm.can_split(s.start):
            cuts.setdefault(si, []).append(off)
    return all(_assign_group(tm, tm.segs[si], offs, subs) for si, offs in cuts.items())


def _assign_group(tm: TextMap, seg: text_map.Seg, offs: list[int], subs: list[Word]) -> bool:
    if seg.in_k:
        return False  # natural runs over the reading here, so the kanji split is the unknown
    bounds = [0] + sorted(offs) + [len(seg.natural)]
    parts = list(zip(bounds, bounds[1:]))
    options = []
    for i, (a, b) in enumerate(parts):
        s = next((s for s in subs if s.start <= seg.nat_start + a < s.end), None)
        readings = _piece_readings(tm, s, seg, a, b, rendaku=i > 0) if s else []
        if not readings:
            return False
        options.append(readings)
    for combo in itertools.product(*options):
        if "".join(combo) == to_hiragana(seg.reading):
            for (a, b), reading in zip(parts, combo):
                tm.set_piece_reading(seg, a, b, seg.base[a:b], reading)
            return True
    return False


def _piece_readings(
    tm: TextMap, w: Word, seg: text_map.Seg, a: int, b: int, rendaku: bool
) -> list[str]:
    """What the sub-word could be reading at natural offsets [a, b) of the group: its own
    reading, Sudachi's and JMdict's, less the kana it has outside the group (入り -> いり -> い).
    A piece after the first may be voiced by the compound (買[かい] -> 買[がい])."""
    before = tm.natural[w.start : seg.nat_start + a]
    after = tm.natural[seg.nat_start + b : w.end]
    if KANJI_RE.search(before + after):
        return []  # the word reaches into another group, whose share is unknown too
    before, after = to_hiragana(before), to_hiragana(after)
    written = tm.written_form(w.start, w.end)
    candidates = ["".join(m.reading for m in w.morphs)] + list(jmdict.readings(written))
    out = []
    for cand in dict.fromkeys(to_hiragana(c) for c in candidates):
        piece = cand[len(before) : len(cand) - len(after)]
        if not cand.startswith(before) or not cand.endswith(after) or not piece:
            continue
        for form in [piece, voiced(piece)] if rendaku else [piece]:
            if form not in out and not KANJI_RE.search(form):
                out.append(form)
    return out


# --- 7. dictionary form, reading, part of speech -------------------------------------------

VERB_STEM_TO_DICT = {
    "い": "う",
    "き": "く",
    "ぎ": "ぐ",
    "し": "す",
    "ち": "つ",
    "に": "ぬ",
    "び": "ぶ",
    "み": "む",
    "り": "る",
}


def _common_suffix(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def noun_form_verb(written: str, w: Word) -> Optional[str]:
    """囁き -> 囁く: a noun that is a godan verb's ます-stem is listed as the verb (old prompt
    rule). `_noun_form_verb` keeps a lexicalized noun of its own (積り) as the noun."""
    if (
        w.head.pos[0] not in ("名詞", "接尾辞")
        or len(w.morphs) != 1
        or not KANJI_RE.search(written)
    ):
        return None
    if written[-1] not in VERB_STEM_TO_DICT or not KANJI_RE.match(written[0]):
        return None
    verb = written[:-1] + VERB_STEM_TO_DICT[written[-1]]
    return verb if jmdict.has_pos(verb, "v5") else None


# く-forms kept as written instead of going back to their adjective: adverbs whose meaning is
# their own (危うく "nearly", not 危うい "dangerous"). JMdict can't tell them apart, as it lists
# 大きく and 早く as adverbs too; Sudachi has most such adverbs as 副詞 already (恐らく, 暫く),
# and those only need listing here when their stem + い reads as an adjective (全く, 全い).
LEXICAL_KU_ADVERBS = {"危うく", "全く"}
# Tokens that are an i-adjective form without being tokenized as one: 良く as an adverb,
# 大きな as an adnominal, 多く (多くの) and 近く (近くで) as nouns
ADJECTIVE_FORM_ENDINGS = {"副詞": "く", "連体詞": "な", "名詞": "く"}


def adjective_of(tm: TextMap, w: Word) -> Optional[str]:
    """The i-adjective a one-token word is a form of: <k>良く</k> (副詞) -> 良い, 大きな
    (連体詞) -> 大きい, 近く (名詞) -> 近い. JMdict must read the adjective as the word's stem,
    so 正しく read まさしく is not 正しい. A noun needs a kanji: kana ones are pieces of
    something else (the しく of <k>正しく</k>, みるく)."""
    if w.kind != "word" or len(w.morphs) != 1 or w.jm_form:
        return None
    ending = ADJECTIVE_FORM_ENDINGS.get(w.head.pos[0])
    written = tm.written_form(w.start, w.end)
    if not ending or not written.endswith(ending) or written in LEXICAL_KU_ADVERBS:
        return None
    if w.head.pos[0] == "名詞" and not KANJI_RE.search(written):
        return None
    adjective = written[:-1] + "い"
    reading = w.head.reading[:-1] + "い"
    for _, readings, pos in jmdict.lookup(adjective):
        if "adj-i" in pos and reading in {to_hiragana(r) for r in readings}:
            return adjective
    return None


def _surface_form(tm: TextMap, w: Word) -> str:
    """The word as the note writes it, numbers as numerals (１日 -> 一日)."""
    value = number_value(tm, w)
    if value is not None:
        return numbers.numeral(value)
    if w.kind == "expression" and w.subs:
        return "".join(_surface_form(tm, s) for s in w.subs)
    return tm.written_form(w.start, w.end)


def _has_unread_number(tm: TextMap, w: Word) -> bool:
    """Whether the word is or contains a number without furigana."""
    if w.kind == "expression":
        return any(_has_unread_number(tm, s) for s in w.subs)
    return number_value(tm, w) is not None and (
        tm.written_form(w.start, w.end) == tm.surface_reading(w.start, w.end)
    )


def _surface_reading(tm: TextMap, w: Word) -> str:
    if number_value(tm, w) is not None:
        return dict_reading(tm, w)
    if w.kind == "expression" and w.subs:
        return "".join(_surface_reading(tm, s) for s in w.subs)
    return _furigana_reading(tm, w.start, w.end, w.morphs)


def dict_form(tm: TextMap, w: Word) -> str:
    form = _dict_form(tm, w)
    if tm.k_as_kana or jmdict.lookup(form):
        return form
    # A word partly inside <k> mixes the note's kanji with kanji kanjify_sentence put on kana,
    # which JMdict may not spell together: 対[たい]<k>為[す]る</k> -> に対為る, 日当[ひあ]<k>当[た]り
    # -> 日当当り. Those take the <k> part as kana when JMdict has that spelling (に対する)
    furi = [s for s in tm.segs_of(w.start, w.end) if s.kind == "furi"]
    if not any(s.in_k for s in furi) or all(s.in_k for s in furi):
        return form
    kana = _dict_form(replace(tm, k_as_kana=True), w)
    return kana if jmdict.lookup(kana) else form


def _dict_form(tm: TextMap, w: Word) -> str:
    if w.kind == "expression":
        # In the note's spelling, whichever spelling JMdict matched (様に成る, not ようになる)
        if w.surface_match:
            return _surface_form(tm, w)
        return "".join(_surface_form(tm, s) for s in w.subs[:-1]) + dict_form(tm, w.subs[-1])
    if w.jm_form:
        return w.jm_form
    value = number_value(tm, w)
    if value is not None:
        return numbers.numeral(value)
    head = w.head
    written = tm.written_form(w.start, w.end)
    if is_copula(head):
        return head.surface if head.surface in PARTICLE_COPULA else "だ"
    if head.lemma == "する" and len(w.morphs) > 1 and w.morphs[1].lemma == "れる":
        return ("為" if written.startswith("為") else "さ") + "れる"
    if (
        head.pos[0] in INFLECTING
        and not KANJI_RE.match(written[-1:])
        and jmdict.has_pos(written, "exp")
        and not w.followed_by_verb
        # An inflection that happens to be an expression too stays the verb: 出来た, 行けません
        and not any(m.pos[0] == "助動詞" for m in w.morphs[1:])
    ):
        return written  # 下さい, 於いて (not 連れて in 連れて行く)
    if len(w.morphs) == 1 and written in LEXICAL_KU_ADVERBS:
        return written  # 危うく
    adjective = adjective_of(tm, w)
    if adjective:
        return adjective
    verb = _noun_form_verb(tm, w)
    if verb:
        return verb
    if len(w.morphs) == 1 and head.pos[0] not in INFLECTING and head.lemma == head.surface:
        # A word written in kana gets its kanji spelling (いった -> 言う, なに -> 何)
        in_k = any(s.in_k for s in tm.segs_of(w.start, w.end))
        if (
            not KANJI_RE.search(written)
            and not in_k
            and (KANJI_RE.search(head.norm) or head.surface == "ん")
        ):
            return head.norm
        return written
    if not KANJI_RE.search(written) and head.pos[0] in INFLECTING:
        return head.norm
    # Keep the note's own kanji spelling: written stem + the lemma's kana tail
    wr_head = tm.written_form(head.start, head.end)
    c = _common_suffix(wr_head, head.surface)
    w_stem, n_stem = wr_head[: len(wr_head) - c], head.surface[: len(head.surface) - c]
    lemma = head.lemma
    # Potential forms: normalized_form de-potentializes (行ける -> 行く) but also swaps kanji
    # (聴く -> 聞く), so it's only taken when it starts with the same character
    if head.pos[0] == "動詞" and head.norm != head.lemma and jmdict.lookup(head.norm):
        if _respell(w_stem, n_stem, head.norm)[:1] == _respell(w_stem, n_stem, lemma)[:1]:
            lemma = head.norm
    if not n_stem or lemma.startswith(n_stem):
        respelled = _respell(w_stem, n_stem, lemma)
        # The note's kanji can belong to another word: <k>呉[くだ]さい</k> is 下さる, as 呉さる
        # reads くれさる. Kanji read the way the text reads them stay, JMdict entry or not
        # (拓ける, ひらける)
        if (
            respelled != head.norm
            and not _sudachi_reading(respelled).startswith(to_hiragana(n_stem))
            and not jmdict.lookup(respelled)
            and jmdict.lookup(head.norm)
        ):
            return head.norm
        return respelled
    return head.norm


def _respell(w_stem: str, n_stem: str, lemma: str) -> str:
    return w_stem + lemma[len(n_stem) :] if n_stem and lemma.startswith(n_stem) else lemma


@lru_cache(maxsize=4096)
def _sudachi_reading(text: str) -> str:
    return to_hiragana("".join(m.reading_form() for m in _tokenizer().tokenize(text)))


def number_value(tm: TextMap, w: Word) -> Optional[int]:
    """The value of a number word (1, １, 二十八, 千九百三十五), None for any other word."""
    if len(w.morphs) != 1 or w.head.pos[:2] != ("名詞", "数詞"):
        return None
    return numbers.parse_number(tm.written_form(w.start, w.end))


def _furigana_reading(tm: TextMap, start: int, end: int, morphs: list[Morph]) -> str:
    furi = to_hiragana(tm.surface_reading(start, end))
    if KANJI_RE.search(furi):
        # Kanji without furigana, or part of a group that can't be split: Sudachi's reading
        furi = "".join(m.reading for m in morphs)
    return furi


def _jmdict_suffix(tm: TextMap, w: Word) -> bool:
    """Whether a Sudachi suffix is one JMdict lists as spelled and read here: 振り read ぶり, 通し
    read どおし and 張り read ばり are suffixes of their own, not forms of 振る, 通す and 張る."""
    if w.head.pos[0] != "接尾辞" or len(w.morphs) != 1:
        return False
    reading = to_hiragana(_furigana_reading(tm, w.start, w.end, w.morphs))
    return any(
        reading in {to_hiragana(r) for r in rs} and "suf" in ps  # not n-suf: 付き, 合い, 持ち
        for _, rs, ps in jmdict.lookup(tm.written_form(w.start, w.end))
    )


def _noun_form_verb(tm: TextMap, w: Word) -> Optional[str]:
    """The verb a sub-word noun or suffix is the stem of (買い of 買い物 -> 買う), unless it is a
    する-noun here (寝返り為る), a JMdict suffix of its own (振り[ぶり]) or read as no form of the
    verb (黙り[だんまり] is not 黙る). A word of its own is mostly a lexicalized noun (動き, 周り,
    嫌い), so it stays the noun if JMdict has one spelled and read so."""
    if w.followed_by_suru or _jmdict_suffix(tm, w):
        return None
    written = tm.written_form(w.start, w.end)
    verb = noun_form_verb(written, w)
    if not verb:
        return None
    reading = to_hiragana(_furigana_reading(tm, w.start, w.end, w.morphs))
    verb_reading = reading[:-1] + VERB_STEM_TO_DICT.get(reading[-1:], "")
    if not {verb_reading, unvoiced(verb_reading)} & {to_hiragana(r) for r in jmdict.readings(verb)}:
        return None
    if not w.nested and any(
        written in kebs and reading in {to_hiragana(r) for r in rebs}
        for kebs, rebs, _ in jmdict.lookup(written)
    ):
        return None
    return verb


def _own_share(tm: TextMap, w: Word, furi: str) -> str:
    """The word's own part of furigana the note put on it for the text before it too: 空</b>域[くう
    いき], ネット上[ねっとじょう], 無人</b>島[むじんとう]. When the reading is none of the word's
    own but ends in one, and the rest is how the text right before it reads, that part is taken."""
    written = tm.written_form(w.start, w.end)
    known = {to_hiragana(r) for r in jmdict.readings(written)} | {_sudachi_reading(written)}
    if not KANJI_RE.search(written) or furi in known:
        return furi
    for own in sorted(known, key=len, reverse=True):
        if not own or len(own) >= len(furi) or not furi.endswith(own):
            continue
        lead = furi[: len(furi) - len(own)]
        for start in range(w.start - 1, max(w.start - 8, 0) - 1, -1):
            text = to_hiragana(tm.surface_reading(start, w.start))
            if text == lead or (
                KANJI_RE.search(text) and _sudachi_reading(tm.written_form(start, w.end)) == furi
            ):
                return own
    return furi


def dict_reading(tm: TextMap, w: Word) -> str:
    head = w.head
    value = number_value(tm, w)
    if value is not None:
        furi = to_hiragana(tm.surface_reading(w.start, w.end))
        # The note's furigana (一[ひと]つ) where it reads the whole number; numbers mostly have
        # none, or only on their units (１万[まん]２千[せん])
        if tm.written_form(w.start, w.end) != furi and not UNREAD_RE.search(furi):
            return furi
        return numbers.number_reading(value)
    if w.kind == "expression":
        if w.surface_match:
            reading = _surface_reading(tm, w)
            known = [to_hiragana(r) for r in w.jm_readings]
            if known and reading not in known and (_has_unread_number(tm, w) or not w.subs):
                # A number the note gives no reading for, before a counter: 三つ is みっつ,
                # not さん + つ; or words whose Sudachi readings didn't add up to JMdict's, and
                # so were dropped: 一日中 is いちにちじゅう, not いち + にっちゅう
                return max(known, key=lambda r: _common_prefix(r, reading))
            return reading
        prefix = "".join(_surface_reading(tm, s) for s in w.subs[:-1])
        last = dict_reading(tm, w.subs[-1])
        known = {to_hiragana(r) for r in w.jm_readings}
        if prefix + last not in known and prefix + voiced(last) in known:
            # The last word keeps the compound's rendaku, which its own dictionary reading
            # doesn't have: 義務[ぎむ] 付[づ]けられる is ぎむづける, though 付ける is つける
            return prefix + voiced(last)
        return prefix + last
    if is_copula(head):  # after expressions, which can start on one: で有る
        return head.surface if head.surface in PARTICLE_COPULA else "だ"
    furi = _furigana_reading(tm, w.start, w.end, w.morphs)
    lemma = dict_form(tm, w)
    written = tm.written_form(w.start, w.end)
    if lemma == written:
        # Uninflected: the note's furigana is the reading, less any rendaku from the compound
        # the word was split out of (閏日 -> 日[び] -> ひ)
        if not KANJI_RE.search(tm.surface_reading(w.start, w.end)):
            furi = _own_share(tm, w, furi)
        if KANJI_RE.search(written) and unvoiced(furi) != furi:
            known = {to_hiragana(r) for r in jmdict.readings(written)}
            if furi not in known and unvoiced(furi) in known:
                return unvoiced(furi)
        return furi
    if head.lemma == "する" and lemma.endswith("れる"):
        return "される"
    if head.lemma in ("来る", "くる") and head.surface in ("来", "き", "こ", "く", "来る", "くる"):
        return "くる"  # irregular stem: き/こ would otherwise pick きたる by prefix
    candidates = [to_hiragana(r) for r in w.jm_readings]
    candidates += [to_hiragana(r) for r in jmdict.readings(lemma)]
    candidates.append(_sudachi_reading(lemma))
    if furi in candidates:
        return furi
    # The candidate agreeing longest with the furigana stem wins; ties keep JMdict order
    return max(candidates, key=lambda r: _common_prefix(r, furi))


POS_MAP = [
    (("名詞", "普通名詞"), "noun"),
    (("名詞", "固有名詞"), "proper noun"),
    (("名詞", "数詞"), "number"),
    (("代名詞",), "pronoun"),
    (("動詞",), "verb"),
    (("形容詞",), "adjective"),
    (("形状詞",), "na-adjective"),
    (("副詞",), "adverb"),
    (("連体詞",), "adjectival"),
    (("助詞",), "particle"),
    (("接続詞",), "conjunction"),
    (("接頭辞",), "prefix"),
    (("接尾辞",), "suffix"),
    (("感動詞",), "interjection"),
]
JM_POS_MAP = [
    ("exp", "expression"),
    ("adv", "adverb"),
    ("v", "verb"),
    ("adj-i", "adjective"),
    ("adj-na", "na-adjective"),
    ("pn", "pronoun"),
    ("n", "noun"),
]


SUFFIX_JM_POS = ("suf", "n-suf", "ctr")
# vs (a する-noun), vt and vi (transitivity) are no verb of their own: 供[とも] is n, vs, vt
NON_VERB_V_POS = ("vs", "vt", "vi")
# Label of a word Sudachi calls a suffix that JMdict, with its reading, has only as a word of its
# own; noun before pronoun and adverb, which 家[うち] and 中[なか] also are
NOT_SUFFIX_POS_MAP = [
    ("prt", "particle"),
    ("v", "verb"),
    ("n", "noun"),
    ("adj-i", "adjective"),
    ("adj-na", "na-adjective"),
    ("pn", "pronoun"),
    ("adv", "adverb"),
]


def _not_suffix_label(form: str, reading: str) -> Optional[str]:
    """JMdict's label for a Sudachi suffix JMdict doesn't list as one with this reading: 家[うち]
    after 一日中, 的[まと]に, 等[など], a verb stem like 沿い. None keeps it a suffix."""
    codes = {
        p
        for _, rs, ps in jmdict.lookup(form)
        if reading in {to_hiragana(r) for r in rs}
        for p in ps
    }
    if not codes or codes & set(SUFFIX_JM_POS):
        return None
    for code, label in NOT_SUFFIX_POS_MAP:
        if any(
            p == code or (code == "v" and p.startswith("v") and p not in NON_VERB_V_POS)
            for p in codes
        ):
            return label
    return None


def pos_label(
    tm: TextMap, w: Word, prev: Optional[Word], reading: str, nested: bool = False
) -> str:
    if w.kind == "expression" or w.jm_pos:  # a JMdict match, or a furigana merge JMdict has
        for code, label in JM_POS_MAP:
            if any(p == code or (code == "v" and p.startswith("v")) for p in w.jm_pos):
                return label
        return "expression"
    if len(w.morphs) == 1 and tm.written_form(w.start, w.end) in LEXICAL_KU_ADVERBS:
        return "adverb"
    if adjective_of(tm, w):
        return "adjective"
    h = w.head
    if is_copula(h):
        return "particle" if h.surface in PARTICLE_COPULA else "copula"
    if h.pos[0] == "助動詞":
        return "auxiliary"
    if (
        h.pos[0] in ("接尾辞", "名詞")
        and len(w.morphs) == 1
        and prev is not None
        and all(m.pos[:2] == ("名詞", "数詞") for m in prev.morphs)  # not 一日中 家
    ):
        return "counter"  # 隻, and nouns counting after a number: 3 月, 1935 年
    verb = _noun_form_verb(tm, w)
    if verb and dict_form(tm, w) == verb:
        return "verb"  # a stem listed as its verb: 買い of 買い物, 手抜き's 抜き
    if h.pos[0] == "接尾辞" and len(w.morphs) == 1:
        label = _not_suffix_label(dict_form(tm, w), reading)
        # Inside a compound Sudachi's suffix is a bound piece JMdict may tag only as a noun (官 of
        # 警察官); only a verb stem there is relabelled, being a verb by its dict_form already
        if label and (not nested or label == "verb"):
            return label
    for key, label in POS_MAP:
        if h.pos[: len(key)] == key:
            return label
    return h.pos[0]


# --- assembly ------------------------------------------------------------------------------

TAG_OR_TEXT_RE = re.compile(r"<[^>]+>|[^<]+")
OPEN_CLOSE_TAG_RE = re.compile(r"<(/?)([a-zA-Z]+)[^>]*>")


def _close_open_tags(raw: str, rs: int, re_: int, limit: int) -> int:
    """Extend a span over closing tags directly after it that close tags it opened
    (" 間[ま]も<k> 無[な]く" + "</k>"), so raw_text stays balanced html where possible."""
    opened = []
    for m in OPEN_CLOSE_TAG_RE.finditer(raw[rs:re_]):
        if not m.group(1):
            opened.append(m.group(2))
        elif opened and opened[-1] == m.group(2):
            opened.pop()
    while opened:
        closing = re.match(rf"</{opened[-1]}>", raw[re_:limit])
        if not closing:
            break
        re_ += closing.end()
        opened.pop()
    return re_


def _emit(
    tm: TextMap, words: list[Word], raw_lo: int, raw_hi: int, nested: bool = False
) -> list[list]:
    """Elements for words within raw[raw_lo:raw_hi]; tags between words are their own elements."""
    out: list[list] = []
    cursor = raw_lo
    prev = None
    for w in words:
        rs, re_ = tm.raw_span(w.start, w.end)
        re_ext = _close_open_tags(tm.raw, rs, re_, raw_hi)
        out += [[p] for p in TAG_OR_TEXT_RE.findall(tm.raw[cursor:rs])]
        raw_text = tm.raw_piece(w.start, w.end) + tm.raw[re_:re_ext]
        if w.kind == "punct":
            out.append([raw_text])
        else:
            w.nested = nested
            subs = _emit(tm, w.subs, rs, re_ext, nested=True) if w.subs else []
            form, reading = dict_form(tm, w), dict_reading(tm, w)
            pos = pos_label(tm, w, prev, reading, nested)
            match_data = match_flags.default_match_data(pos, form, subs)
            out.append([raw_text, pos, form, reading, match_data, subs])
        cursor = re_ext
        prev = w
    out += [[p] for p in TAG_OR_TEXT_RE.findall(tm.raw[cursor:raw_hi])]
    return out


def analyze(sentence: str) -> Analysis:
    tm = text_map.build(sentence)
    words = merge_cut_groups(tm, to_words(tokenize(tm.natural)))
    for a, b in zip(words, words[1:]):
        a.followed_by_verb = b.head.pos[0] == "動詞"
        a.followed_by_suru = b.head.lemma == "する"  # 寝返り為る stays a noun
    cands = jmdict_candidates(tm, words)
    final = nest(words, choose_matches(cands, words))
    split_group_readings(tm, final)
    add_decompositions(tm, final)
    return Analysis(tm, words, cands, final, _emit(tm, final, 0, len(tm.raw)))


def generate(sentence: str) -> list[list]:
    """The word array for a furigana sentence (html allowed, <b> tags dropped)."""
    return analyze(sentence).array
