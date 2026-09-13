"""Map a furigana + html sentence to tokenizer input text and back.

The raw sentence is split into segments:
  tag   <k>, </k>, <div> ... (<b> tags are dropped, as word arrays never contain them)
  furi  " 漢字[よみ]" group; in_k is True inside <k>...</k>
  char  any other single character (kana, punctuation, digits, a stray space)

`natural` is the text given to the tokenizer. Furigana groups inside <k> are reverted to their
reading, since <k> marks words that were kana before kanjify_sentence ran: the tokenizer then
sees the sentence as originally written, which avoids misreadings like 遣っ -> 遣う (for やる)
or 為れ -> なる (for される). Other groups contribute their kanji base.

Every natural char remembers (segment index, offset in that segment's natural text), so any
token span can be turned back into raw text. Top-level words always cover whole furigana
groups; a sub-word may end inside one (天高[てんたか]く -> 天 + 高く), in which case its raw
text is rebuilt with that group's reading split per kanji (" 天[てん]" + "高[たか]く"). A
jukujikun group has no per-kanji split; there the generator works the pieces out from the
sub-words' own readings and records them with set_piece_reading.
"""

import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Callable, Iterable, Optional

from ..kana_conv import to_hiragana
from ..shared.jp_text_processing.all_types.main_types import WithTagsDef
from ..shared.jp_text_processing.kana.kana_highlight import kana_highlight

B_TAG_RE = re.compile(r"</?b>")
SEG_RE = re.compile(r"(<[^>]+>)|( ?)([^ <>\[\]]+)\[([^\]]*)\]|(.)", re.S)
KANJI_TAIL_RE = re.compile(r"[\d々ヶヵ一-龯㐀-䶿]+$")
TAG_RE = re.compile(r"<[^>]+>")
FURI_BRACKET_RE = re.compile(r"\[[^\]]*\]")
KATAKANA_RE = re.compile(r"[ァ-ヺー]+")
HIRAGANA_RE = re.compile(r"[ぁ-ゖ]")
# kana_highlight's per-kanji output: <on> 世[せ]</on><kun> 高[たか]</kun><oku>く</oku>
READING_UNIT_RE = re.compile(r"<(on|kun|juk)> ?([^<\[]+)\[([^\]]*)\]</\1>")
SPLIT_READINGS = WithTagsDef(True, False, False, False)


@dataclass
class Seg:
    kind: str  # tag | furi | char
    raw: str
    raw_start: int
    raw_end: int
    base: str = ""  # furi: written base (kanji, maybe with kana); char: the char
    reading: str = ""  # furi only
    in_k: bool = False
    natural: str = ""  # what this segment contributes to the tokenizer text
    idx: int = 0  # position in TextMap.segs
    nat_start: int = 0  # natural index this segment's text starts at
    reads_kanji: bool = False  # a <k> group given to the tokenizer as its kanji (read_as_kanji)

    @property
    def natural_is_reading(self) -> bool:
        return self.in_k and not self.reads_kanji

    @property
    def lead(self) -> str:
        return self.raw[: len(self.raw) - len(self.raw.lstrip(" "))]


@lru_cache(maxsize=4096)
def reading_units(base: str, reading: str) -> Optional[tuple[tuple[str, str, str], ...]]:
    """Split a furigana group's reading per kanji, with each kanji's reading type:
    ("見下", "みお") -> (("見", "み", "kun"), ("下", "お", "kun")).

    None when the group can't be split: jukujikun (今日[きょう]), or a base that isn't all kanji.
    """
    if not KANJI_TAIL_RE.fullmatch(base):
        return None
    tagged = kana_highlight(None, f" {base}[{reading}]", "furigana", SPLIT_READINGS)
    units = [(m.group(2).strip(), m.group(3), m.group(1)) for m in READING_UNIT_RE.finditer(tagged)]
    if (
        not units
        or "<juk>" in tagged
        or "".join(u[0] for u in units) != base
        or "".join(u[1] for u in units) != reading
    ):
        return None
    return tuple(units)


@dataclass
class TextMap:
    raw: str
    segs: list[Seg]
    natural: str
    # natural index -> (seg index, offset within seg.natural)
    nat_pos: list[tuple[int, int]] = field(default_factory=list)
    # (seg index, a, b) -> (written, reading) for a piece of a group that doesn't split per
    # kanji, worked out from the sub-words by generator.split_group_readings
    piece_readings: dict[tuple[int, int, int], tuple[str, str]] = field(default_factory=dict)
    # written_form() gives <k> groups as the kana they were before kanjify_sentence ran
    k_as_kana: bool = False

    def boundary_ok(self, nat_idx: int) -> bool:
        """True when a word boundary before natural index nat_idx doesn't cut a furigana group."""
        if nat_idx <= 0 or nat_idx >= len(self.natural):
            return True
        return self.nat_pos[nat_idx][1] == 0

    def raw_span(self, start: int, end: int) -> tuple[int, int]:
        """Raw offsets for natural span [start, end). A span starting inside a furigana group
        begins after the base chars before it; one ending inside a group stops before the
        bracket. Consecutive spans therefore tile the raw text, which is what the gaps between
        words (tags) are computed from; raw_piece() gives the text to store."""
        si, so = self.nat_pos[start]
        seg = self.segs[si]
        if seg.kind == "furi" and so > 0:
            raw_s = seg.raw_start + len(seg.lead) + self._base_chars_before(seg, so)
        else:
            raw_s = seg.raw_start
        ei, eo = self.nat_pos[end - 1]
        seg = self.segs[ei]
        if seg.kind == "furi" and eo < len(seg.natural) - 1:
            raw_e = seg.raw_start + len(seg.lead) + self._base_chars_before(seg, eo + 1)
        else:
            raw_e = seg.raw_end
        return raw_s, raw_e

    def raw_piece(self, start: int, end: int) -> str:
        """The raw text of natural span [start, end), with any partly covered furigana group
        given its own share of the reading."""
        si, so = self.nat_pos[start]
        ei, eo = self.nat_pos[end - 1]
        out = ""
        for idx in range(si, ei + 1):
            seg = self.segs[idx]
            a = so if idx == si else 0
            b = eo + 1 if idx == ei else len(seg.natural)
            if seg.kind == "furi" and (a > 0 or b < len(seg.natural)):
                out += self._furi_piece(seg, a, b)
            else:
                out += seg.raw
        return out

    def piece_reading(self, seg: Seg, a: int, b: int) -> Optional[str]:
        """Reading of natural offsets [a, b) of a furigana group, when it can be split there."""
        if a == 0 and b == len(seg.natural):
            return seg.reading
        units = self._unit_slice(seg, a, b)
        if units is not None:
            return "".join(u[1] for u in units)
        given = self.piece_readings.get((seg.idx, a, b))
        return given[1] if given is not None else None

    def set_piece_reading(self, seg: Seg, a: int, b: int, written: str, reading: str) -> None:
        """Record how a group that doesn't split per kanji splits between two sub-words."""
        self.piece_readings[(seg.idx, a, b)] = (written, reading)

    def _is_split_point(self, seg: Seg, off: int) -> bool:
        return any(k[0] == seg.idx and off in k[1:] for k in self.piece_readings)

    def _unit_slice(self, seg: Seg, a: int, b: int) -> Optional[tuple[tuple[str, str, str], ...]]:
        units = reading_units(seg.base, seg.reading)
        if units is None:
            return None
        # Natural offsets run over the reading in <k> groups, over the base otherwise
        bounds = [0]
        for kanji, reading, _ in units:
            bounds.append(bounds[-1] + len(reading if seg.natural_is_reading else kanji))
        if a not in bounds or b not in bounds:
            return None
        return units[bounds.index(a) : bounds.index(b)]

    def can_split(self, nat_idx: int) -> bool:
        """True when a sub-word boundary can go before natural index nat_idx: between
        segments, inside a furigana group where its reading splits per kanji, or where
        set_piece_reading has already worked the group's split out."""
        si, off = self.nat_pos[nat_idx]
        if off == 0:
            return True
        seg = self.segs[si]
        if seg.kind != "furi":
            return False
        return self._unit_slice(seg, 0, off) is not None or self._is_split_point(seg, off)

    def is_onyomi_kanji(self, start: int, end: int) -> bool:
        """True when natural span [start, end) is a single kanji read in on'yomi."""
        si, off = self.nat_pos[start]
        seg = self.segs[si]
        if seg.kind != "furi" or self.nat_pos[end - 1][0] != si:
            return False
        units = self._unit_slice(seg, off, end - start + off)
        return units is not None and len(units) == 1 and units[0][2] == "on"

    def _furi_piece(self, seg: Seg, a: int, b: int) -> str:
        lead = seg.lead if a == 0 else ""
        units = self._unit_slice(seg, a, b)
        if units is not None:
            return f"{lead}{''.join(u[0] for u in units)}[{''.join(u[1] for u in units)}]"
        given = self.piece_readings.get((seg.idx, a, b))
        if given is not None:
            return f"{lead}{given[0]}[{given[1]}]"
        # No reading for this piece: the bracket stays whole on the group's last piece. Words
        # are only split where a reading is known, so this is a fallback for callers that cut
        # a group on their own (research/validate.py wrapping arbitrary spans).
        ka = self._base_chars_before(seg, a)
        kb = self._base_chars_before(seg, b) if b < len(seg.natural) else len(seg.base)
        bracket = f"[{seg.reading}]" if b == len(seg.natural) else ""
        return f"{lead}{seg.base[ka:kb]}{bracket}"

    def _base_chars_before(self, seg: Seg, nat_off: int) -> int:
        """How many base chars correspond to the first nat_off natural chars of a furi seg."""
        if not seg.natural_is_reading:
            return nat_off  # natural == base
        units = reading_units(seg.base, seg.reading)
        if units is not None:
            pos = 0
            for i, (_, reading, _) in enumerate(units):
                if pos >= nat_off:
                    return i
                pos += len(reading)
            return len(units)
        # No alignment: split the base proportionally, keeping a char on each side
        n = len(seg.base)
        return max(1, min(n - 1, round(nat_off * n / len(seg.natural))))

    def surface_reading(self, start: int, end: int) -> str:
        """Reading of natural span [start, end) from the note's furigana. Chars without
        furigana, or part of a group that can't be split, come back as their base text."""
        out = ""
        i = start
        while i < end:
            si, off = self.nat_pos[i]
            seg = self.segs[si]
            if seg.kind == "furi" and not seg.natural_is_reading:
                stop = min(end - i + off, len(seg.natural))
                part = self.piece_reading(seg, off, stop)
                out += part if part is not None else seg.natural[off:stop]
                i += stop - off
            else:
                out += seg.reading[off] if seg.kind == "furi" else seg.natural[off]
                i += 1
        return out

    def written_form(self, start: int, end: int) -> str:
        """The written (kanjified) surface of a natural span, tags and furigana stripped."""
        if self.k_as_kana:
            # <k> groups contribute their reading there, but for those read as kanji
            out = ""
            i = start
            while i < end:
                si, off = self.nat_pos[i]
                seg = self.segs[si]
                if not seg.reads_kanji:
                    out += self.natural[i]
                    i += 1
                    continue
                stop = min(end - i + off, len(seg.natural))
                part = self.piece_reading(seg, off, stop)
                out += part if part is not None else seg.natural[off:stop]
                i += stop - off
            return out
        rs, re_ = self.raw_span(start, end)
        text = FURI_BRACKET_RE.sub("", TAG_RE.sub("", self.raw[rs:re_]))
        return text.replace(" ", "")

    def segs_of(self, start: int, end: int) -> list[Seg]:
        return [self.segs[i] for i in sorted({self.nat_pos[j][0] for j in range(start, end)})]


def _char_seg(ch: str, pos: int) -> Seg:
    seg = Seg("char", ch, pos, pos + 1, base=ch)
    seg.natural = "" if ch == " " else ch
    return seg


def build(
    raw_sentence: str, hiragana_reading: Optional[Callable[[str, str], bool]] = None
) -> TextMap:
    """hiragana_reading(reading, rest) says whether a <k> group's katakana furigana, followed by
    the group's hiragana `rest`, goes to the tokenizer in hiragana."""
    # <b> is parsed as a tag so it still delimits furigana bases ("二<b>隻[せき]</b>"), then left
    # out, so raw offsets refer to the <b>-free sentence.
    segs: list[Seg] = []
    in_k = False
    raw = ""
    for m in SEG_RE.finditer(raw_sentence):
        if m.group(1):
            tag = m.group(1)
            if B_TAG_RE.fullmatch(tag):
                continue
            if tag == "<k>":
                in_k = True
            elif tag == "</k>":
                in_k = False
            segs.append(Seg("tag", tag, len(raw), len(raw) + len(tag)))
            raw += tag
        elif m.group(3) is not None:
            lead, base, reading = m.group(2), m.group(3), m.group(4)
            if not lead:
                # No separator space ("うに開豁[かいかつ]"): the furigana only covers the
                # trailing kanji run
                km = KANJI_TAIL_RE.search(base)
                if km and km.start() > 0:
                    for ch in base[: km.start()]:
                        segs.append(_char_seg(ch, len(raw)))
                        raw += ch
                    base = base[km.start() :]
            text = f"{lead}{base}[{reading}]"
            seg = Seg(
                "furi", text, len(raw), len(raw) + len(text), base=base, reading=reading, in_k=in_k
            )
            seg.natural = reading if in_k else base
            segs.append(seg)
            raw += text
        else:
            segs.append(_char_seg(m.group(5), len(raw)))
            raw += m.group(5)

    # Katakana furigana followed by hiragana in the same <k> group can go in as hiragana, when
    # the tokenizer reads the word better that way (ホめる is a name ホ, suffix め and auxiliary
    # る; ほめる is 褒める). A reading standing alone stays katakana, as ゼロ or コイン are read
    # best as written.
    if hiragana_reading:
        for k, seg in enumerate(segs[:-1]):
            if not (
                seg.kind == "furi"
                and seg.in_k
                and KATAKANA_RE.fullmatch(seg.natural)
                and segs[k + 1].kind == "char"
                and HIRAGANA_RE.match(segs[k + 1].natural)
            ):
                continue
            rest = ""
            for nxt in segs[k + 1 :]:
                if nxt.kind == "tag":
                    break
                rest += nxt.natural
            if hiragana_reading(seg.natural, rest):
                seg.natural = to_hiragana(seg.natural)
    return _assemble(raw, segs)


def read_as_kanji(tm: TextMap, seg_indices: Iterable[int]) -> TextMap:
    """A new text map with these <k> groups given to the tokenizer as their kanji."""
    segs = [replace(seg) for seg in tm.segs]
    for i in seg_indices:
        segs[i].natural, segs[i].reads_kanji = segs[i].base, True
    return _assemble(tm.raw, segs)


def _assemble(raw: str, segs: list[Seg]) -> TextMap:
    natural = ""
    nat_pos: list[tuple[int, int]] = []
    for i, seg in enumerate(segs):
        seg.idx = i
        seg.nat_start = len(natural)
        for off, ch in enumerate(seg.natural):
            natural += ch
            nat_pos.append((i, off))
    return TextMap(raw, segs, natural, nat_pos)
