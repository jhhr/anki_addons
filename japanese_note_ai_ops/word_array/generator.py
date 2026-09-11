"""Word array generator: Sudachi + rule-based grouping + JMdict multi-word candidates.

Output elements are [raw_text, part_of_speech, dict_form, reading, match_data, sub_words];
tags, punctuation and other non-word text are single-element arrays. Concatenating the top-level
raw_text values gives back the sentence without <b> tags.

Stages
  1. text_map: strip tags and furigana, revert <k> words to kana -> natural text
  2. Sudachi, SplitMode.C, with the SplitMode.A split of each compound kept for sub-words
  3. group morphemes into words (a verb/adjective plus its inflection chain)
  4. merge words whose boundary would cut a furigana group (八紘|一宇 -> 八紘一宇)
  5. multi-word candidates: JMdict n-grams (last word also deinflected), and runs of adjacent
     nouns missing from JMdict (proposals only)
  6. choose candidates (keep_candidate: heuristic stand-in, see README)
  7. dictionary form, reading and part of speech per word; readings come from the note's own
     furigana wherever it has them
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from sudachipy import Dictionary, SplitMode

from ..kana_conv import to_hiragana
from . import jmdict_index as jmdict
from . import resources, text_map
from .text_map import TextMap

KANJI_RE = re.compile(r"[一-龯㐀-䶿々]")


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
    jm_form: str = ""
    jm_readings: tuple[str, ...] = ()
    followed_by_verb: bool = False
    followed_by_suru: bool = False

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
    proposal: bool = False  # not a JMdict entry: only the decision step could accept it


@dataclass
class Analysis:
    text_map: TextMap
    words: list[Word]  # after grouping and furigana merges, before candidates
    candidates: list[Candidate]
    final: list[Word]
    array: list[list]

    def sub_word_proposals(self) -> list[tuple[int, int]]:
        return [span for w in self.words for span in decomposition_subs(self.text_map, w)]


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
        mo = _morph(m)
        subs = m.split(SplitMode.A)
        if len(subs) > 1:
            mo.subs = [_morph(s) for s in subs]
        out.append(mo)
    return _split_number_counters(out)


def _split_number_counters(morphs: list[Morph]) -> list[Morph]:
    """'1日' comes out as one token read ついたち; the furigana marks 日 as its own word."""
    out = []
    for m in morphs:
        mm = re.fullmatch(r"(\d+)(\D+)", m.surface)
        if mm and m.pos[0] == "名詞":
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
# Copula forms listed as particles (開豁に, 自由自在な, 気でいる)
PARTICLE_COPULA = ("な", "に", "で")
# Suffixes attaching productively are their own top-level words (様, 達: gold ex. 6)
PRODUCTIVE_SUFFIXES = {
    "様",
    "さま",
    "達",
    "たち",
    "さん",
    "君",
    "くん",
    "ちゃん",
    "殿",
    "ども",
    "共",
    "ら",
}


def is_copula(m: Morph) -> bool:
    return m.pos[0] == "助動詞" and m.norm in ("だ", "です")


def _attaches(prev: Morph, nxt: Morph, head: Morph) -> bool:
    if head.pos[0] not in INFLECTING and head.pos[0] != "助動詞":
        return False
    if nxt.pos[0] == "助動詞":
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
    """Sub-words of a Sudachi compound; the inflection after it goes on the last sub-word."""
    parts = group(g[0].subs)
    parts[-1] = parts[-1] + g[1:]
    return [Word(p) for p in parts]


def to_words(morphs: list[Morph]) -> list[Word]:
    words: list[Word] = []
    for g in group(morphs):
        kind = "punct" if g[0].pos[0] in ("補助記号", "空白") else "word"
        head = g[0]
        if not head.subs:
            words.append(Word(g, kind=kind))
            continue
        subs = _compound_subs(g)
        last_sub = head.subs[-1]
        if last_sub.pos[0] == "接尾辞" and last_sub.surface in PRODUCTIVE_SUFFIXES:
            words.extend(subs)  # 私達 -> 私 + 達
        elif head.pos[0] in ("名詞", "代名詞") and len(g) == 1 and not jmdict.lookup(head.surface):
            words.extend(subs)  # compound unknown to JMdict (遂行能力): its parts are the words
        else:
            words.append(Word(g, kind=kind, subs=subs))
    return words


# --- 4. furigana-cut merge -----------------------------------------------------------------


def merge_cut_groups(tm: TextMap, words: list[Word]) -> list[Word]:
    """A furigana group is never split between top-level words. When the merged text is a JMdict
    word read as the furigana says (業|者, 八紘|一宇) the tokenizer simply split it wrong and the
    pieces aren't sub-words; otherwise (天|高く, 軽音|部, 馬|肥 which JMdict has as うまごやし)
    they are."""
    out: list[Word] = []
    for w in words:
        if out and not tm.boundary_ok(w.start):
            prev = out.pop()
            pieces = (prev.subs if prev.kind == "merged" else [prev]) + [w]
            merged = Word(prev.morphs + w.morphs, kind="merged", subs=pieces)
            written = tm.written_form(merged.start, merged.end)
            hits = jmdict.lookup(written)
            merged.jm_form = written
            merged.jm_readings = tuple(r for rs, _ in hits for r in rs)
            merged.jm_pos = frozenset(p for _, ps in hits for p in ps)
            reading = to_hiragana(tm.surface_reading(merged.start, merged.end))
            if reading in (to_hiragana(r) for r in merged.jm_readings):
                merged.subs = []
            out.append(merged)
        else:
            out.append(w)
    return out


# --- 5. candidates -------------------------------------------------------------------------


def _forms(tm: TextMap, ws: list[Word]) -> list[str]:
    written = [tm.written_form(w.start, w.end) for w in ws]
    natural = [w.natural for w in ws]
    forms = ["".join(written), "".join(natural)]
    last = ws[-1]
    if last.head.pos[0] in INFLECTING:
        forms += [
            "".join(written[:-1]) + dict_form(tm, last),
            "".join(natural[:-1]) + last.head.lemma,
        ]
    return list(dict.fromkeys(forms))


def jmdict_candidates(tm: TextMap, words: list[Word], max_len: int = 8) -> list[Candidate]:
    cands = []
    for i in range(len(words)):
        if words[i].kind == "punct":
            continue
        for j in range(i + 2, min(len(words), i + max_len) + 1):
            if words[j - 1].kind == "punct":
                break
            for form in _forms(tm, words[i:j]):
                hits = jmdict.lookup(form)
                if hits:
                    readings = tuple(r for rs, _ in hits for r in rs)
                    pos = frozenset(p for _, ps in hits for p in ps)
                    cands.append(Candidate(i, j, form, readings, pos))
                    break
    return cands


CONTENT_POS = ("名詞", "代名詞", "形状詞", "接尾辞")


def adjacent_content_candidates(tm: TextMap, words: list[Word]) -> list[Candidate]:
    """Runs of adjacent nouns (声高々, 配役ミス, 遂行能力): proposals only, never auto-accepted."""
    cands = []
    i = 0
    while i < len(words):
        j = i
        while (
            j < len(words)
            and words[j].kind != "punct"
            and len(words[j].morphs) == 1
            and words[j].head.pos[0] in CONTENT_POS
        ):
            j += 1
        for a in range(i, j - 1):
            for b in range(a + 2, j + 1):
                form = tm.written_form(words[a].start, words[b - 1].end)
                cands.append(Candidate(a, b, form, (), frozenset(), proposal=True))
        i = max(j, i + 1)
    return cands


def decomposition_subs(tm: TextMap, w: Word) -> list[tuple[int, int]]:
    """2-way splits of a single word into JMdict words (耳元 -> 耳|元, 正に -> 正|に): sub-word
    proposals as natural spans. Not emitted; over-generates on on'yomi compounds (最|近)."""
    if len(w.morphs) != 1 or w.subs:
        return []
    written = tm.written_form(w.start, w.end)
    if len(written) != len(w.natural) or len(written) < 2:
        return []
    out = []
    for k in range(1, len(written)):
        left, right = written[:k], written[k:]
        if jmdict.lookup(left) and (jmdict.lookup(right) or not KANJI_RE.search(right)):
            out += [(w.start, w.start + k), (w.start + k, w.end)]
    return out


# --- 6. candidate choice -------------------------------------------------------------------


def keep_candidate(c: Candidate, words: list[Word]) -> bool:
    """Heuristic stand-in for the keep/drop decision, following the old extract_words rules."""
    if c.proposal:
        return False
    ws = words[c.i : c.j]
    first, last = ws[0].head, ws[-1].head
    if first.pos[0] == "助詞" and not KANJI_RE.search(c.form):
        return False  # kana-only match starting on a particle (は+いくつ -> はいくつ)
    if all(w.head.pos[0] in ("助詞", "助動詞") for w in ws):
        return False
    if last.pos[0] in ("助詞", "助動詞") and "adv" not in c.pos and "conj" not in c.pos:
        return False  # 様に, には: words ending in particles
    if (
        first.pos[0] == "助詞"
        and c.form.startswith(("に", "で"))
        and "exp" in c.pos
        and c.j - c.i == 2
    ):
        return False  # に於いて
    if any(w.morphs[-1].surface in ("て", "で") and w.head.pos[0] == "動詞" for w in ws[:-1]):
        return False  # て-form + verb (連れて行く) stays two words
    return True


def apply_candidates(words: list[Word], cands: list[Candidate]) -> list[Word]:
    """Greedy longest-first, non-overlapping merge of the kept candidates."""
    taken = [False] * len(words)
    by_start: dict[int, Candidate] = {}
    for c in sorted(cands, key=lambda c: -(c.j - c.i)):
        if any(taken[c.i : c.j]) or not keep_candidate(c, words):
            continue
        by_start[c.i] = c
        taken[c.i : c.j] = [True] * (c.j - c.i)
    out = []
    k = 0
    while k < len(words):
        chosen = by_start.get(k)
        if chosen is None:
            out.append(words[k])
            k += 1
            continue
        c = chosen
        parts = words[c.i : c.j]
        # A furigana merge inside the expression is no word of its own: list its pieces
        subs = [s for p in parts for s in (p.subs if p.kind == "merged" and p.subs else [p])]
        out.append(
            Word(
                [m for p in parts for m in p.morphs],
                kind="expression",
                subs=subs,
                jm_pos=c.pos,
                jm_form=c.form,
                jm_readings=c.readings,
            )
        )
        k = c.j
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
    rule). Wrong for lexicalized nouns (積り), which is a judgement call left to the decision step."""
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


def dict_form(tm: TextMap, w: Word) -> str:
    if w.jm_form:
        return w.jm_form
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
    ):
        return written  # 下さい, 於いて (not 連れて in 連れて行く)
    if (
        head.pos[0] == "形容詞"
        and len(w.morphs) == 1
        and written.endswith("く")
        and jmdict.has_pos(written, "adv")
    ):
        return written  # 危うく
    verb = None if w.followed_by_suru else noun_form_verb(written, w)
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
        return _respell(w_stem, n_stem, lemma)
    return head.norm


def _respell(w_stem: str, n_stem: str, lemma: str) -> str:
    return w_stem + lemma[len(n_stem) :] if n_stem and lemma.startswith(n_stem) else lemma


@lru_cache(maxsize=4096)
def _sudachi_reading(text: str) -> str:
    return to_hiragana("".join(m.reading_form() for m in _tokenizer().tokenize(text)))


DIGIT_R = ["", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]
PLACES = (
    (1000, "せん", {1: "せん", 3: "さんぜん", 8: "はっせん"}),
    (100, "ひゃく", {1: "ひゃく", 3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}),
    (10, "じゅう", {1: "じゅう"}),
)


def number_reading(num: str) -> str:
    n = int(num)
    if n == 0:
        return "ぜろ"
    out = ""
    for unit, name, special in PLACES:
        d, n = divmod(n, unit)
        if d:
            out += special.get(d, DIGIT_R[d] + name)
    return out + DIGIT_R[n]


def dict_reading(tm: TextMap, w: Word) -> str:
    head = w.head
    if re.fullmatch(r"\d+", w.natural):
        return number_reading(w.natural)
    if is_copula(head):
        return head.surface if head.surface in PARTICLE_COPULA else "だ"
    furi = to_hiragana(tm.surface_reading(w.start, w.end))
    if KANJI_RE.search(furi):
        # Kanji without furigana, or part of a group that can't be split: Sudachi's reading
        furi = "".join(m.reading for m in w.morphs)
    lemma = dict_form(tm, w)
    if lemma == tm.written_form(w.start, w.end):
        return furi  # uninflected: the note's furigana is the reading
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


def pos_label(w: Word, prev: Optional[Word]) -> str:
    if w.kind == "expression":
        for code, label in JM_POS_MAP:
            if any(p == code or (code == "v" and p.startswith("v")) for p in w.jm_pos):
                return label
        return "expression"
    h = w.head
    if is_copula(h):
        return "particle" if h.surface in PARTICLE_COPULA else "copula"
    if h.pos[0] == "助動詞":
        return "auxiliary"
    if h.pos[0] == "接尾辞" and prev is not None and prev.head.pos[:2] == ("名詞", "数詞"):
        return "counter"
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


def _emit(tm: TextMap, words: list[Word], raw_lo: int, raw_hi: int) -> list[list]:
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
            subs = _emit(tm, w.subs, rs, re_ext) if w.subs else []
            out.append(
                [raw_text, pos_label(w, prev), dict_form(tm, w), dict_reading(tm, w), [], subs]
            )
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
    cands = jmdict_candidates(tm, words) + adjacent_content_candidates(tm, words)
    final = apply_candidates(words, cands)
    return Analysis(tm, words, cands, final, _emit(tm, final, 0, len(tm.raw)))


def generate(sentence: str) -> list[list]:
    """The word array for a furigana sentence (html allowed, <b> tags dropped)."""
    return analyze(sentence).array
