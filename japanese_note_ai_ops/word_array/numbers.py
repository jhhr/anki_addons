"""Numbers in word arrays.

A number's dictionary form is the Japanese numeral whatever the text writes: 1, １, 1[いち] and
一 are all 一, and 1935 is 千九百三十五 (the raw text keeps its own form, as きったら does for
切る). Numbers are the text most often left without furigana, so their reading is computed
when the note doesn't give one.

Only the numerals that are words of their own get matched to notes: the digits, 十 and 二十,
and the multipliers. Every other number starts out "dontmatch" - numbers have been a steady
source of junk notes.
"""

import unicodedata
from typing import Optional

DIGITS = "〇一二三四五六七八九"
SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
LARGE_UNITS = {"万": 10**4, "億": 10**8, "兆": 10**12}
MATCHED_NUMERALS = frozenset(DIGITS) | {"零", "十", "二十", "百", "千", "万", "億", "兆"}
# What a number written out of numerals can be made of, after NFKC folds the wide digits
NUMERAL_CHARS = (
    frozenset(DIGITS)
    | {"零"}
    | frozenset(SMALL_UNITS)
    | frozenset(LARGE_UNITS)
    | frozenset("0123456789.")
)

DIGIT_READINGS = ["", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]
SMALL_READINGS = (
    (1000, "せん", {3: "さんぜん", 8: "はっせん"}),
    (100, "ひゃく", {3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}),
    (10, "じゅう", {}),
)
LARGE_READINGS = (
    (10**12, "ちょう", {1: "いっちょう", 8: "はっちょう"}),
    (10**8, "おく", {}),
    (10**4, "まん", {}),
)


def parse_number(text: str) -> Optional[int]:
    """The value of a written number - digits of any width, kanji numerals with units or
    written digit by digit (一九三五), or a mix (1万) - or None if text isn't one."""
    text = unicodedata.normalize("NFKC", text)
    if not text:
        return None
    total = group = digits = 0
    has_digits = False
    for ch in text:
        if "0" <= ch <= "9" or ch in DIGITS or ch == "零":
            value = int(ch) if "0" <= ch <= "9" else DIGITS.find(ch) if ch in DIGITS else 0
            digits, has_digits = digits * 10 + value, True
        elif ch in SMALL_UNITS:
            group += (digits if has_digits else 1) * SMALL_UNITS[ch]
            digits, has_digits = 0, False
        elif ch in LARGE_UNITS:
            total += ((group + digits) or 1) * LARGE_UNITS[ch]
            group = digits = 0
            has_digits = False
        else:
            return None
    return total + group + digits


def numeral(n: int) -> str:
    """一, 十, 二十八, 千九百三十五, 一万二千."""
    if n == 0:
        return "〇"
    out = ""
    for name, unit in sorted(LARGE_UNITS.items(), key=lambda item: -item[1]):
        count, n = divmod(n, unit)
        if count:
            out += (_below_10000(count) or "一") + name
    return out + _below_10000(n)


def _below_10000(n: int) -> str:
    out = ""
    for name, unit in sorted(SMALL_UNITS.items(), key=lambda item: -item[1]):
        count, n = divmod(n, unit)
        if count:
            out += ("" if count == 1 else DIGITS[count]) + name
    return out + (DIGITS[n] if n else "")


def number_reading(n: int) -> str:
    if n == 0:
        return "ぜろ"
    out = ""
    for unit, name, special in LARGE_READINGS:
        count, n = divmod(n, unit)
        if count:
            out += special.get(count, _reading_below_10000(count) + name)
    return out + _reading_below_10000(n)


def _reading_below_10000(n: int) -> str:
    out = ""
    for unit, name, special in SMALL_READINGS:
        count, n = divmod(n, unit)
        if count:
            out += special.get(count, ("" if count == 1 else DIGIT_READINGS[count]) + name)
    return out + DIGIT_READINGS[n]


def is_matched_numeral(dict_form: str) -> bool:
    return dict_form in MATCHED_NUMERALS


def is_plain_numeral(dict_form: str) -> bool:
    """Whether the form is written out of nothing but numerals: 二十八, 千九百三十五, 0.5.

    A word the generator labels a number without it being written that way is not one -
    何, 数, 幾, ゼロ, 2人 - and is the judge's to decide like any other word."""
    text = unicodedata.normalize("NFKC", dict_form)
    return bool(text) and all(ch in NUMERAL_CHARS for ch in text)
