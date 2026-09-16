"""Names the relation between a vocab note's reading and the reading of a word array link.

`vocab_unlink` can settle a link that disagrees with its note whenever another vocab note owns
the word. When nobody owns it there is nothing to compare against, and the note was held back.
Most of those held-back cases are not judgement calls at all, though: the two readings are the
same word in two shapes, and which shape is which is regular enough to name.

A family is only ever a description of *how* the readings differ. What to do about it is
`vocab_unlink`'s decision, because the same family means different things there - a deverbal
noun against its verb is a different word and the link should go back to the matcher, while
two readings that differ only by voicing mean one of the two is wrong without saying which.

A family never says which side is right. Voicing in particular runs both ways: the note holds
the rendaku in 狡賢い[ずるがしこい] and 三つ子[みつご], the array holds it in 砂埃[すなぼこり],
and only evidence outside the pair can settle it.

Readings are compared in hiragana; the caller converts.
"""

import re
import unicodedata

# What a godan verb ends in, and the rows its stem takes: 連用形 in the い row, the plain
# negative in the あ row, the potential in the え row.
U_ROW = "うくぐすずつぬふぶむる"
I_ROW = "いきぎしじちにひびみり"
A_ROW = "わかがさざたなはばまら"
E_ROW = "えけげせぜてねへべめれ"
TO_I = dict(zip(U_ROW, I_ROW))
TO_A = dict(zip(U_ROW, A_ROW))
TO_E = dict(zip(U_ROW, E_ROW))

# The 音便 a godan verb takes in its て and た forms, keyed by the kana it ends in.
ONBIN = {
    "く": ("い", "て"),
    "ぐ": ("い", "で"),
    "う": ("っ", "て"),
    "つ": ("っ", "て"),
    "る": ("っ", "て"),
    "ぬ": ("ん", "で"),
    "ぶ": ("ん", "で"),
    "む": ("ん", "で"),
    "す": ("し", "て"),
}

VOICED = "がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽ"
PLAIN = "かきくけこさしすせそたちつてとはひふへほはひふへほ"
DEVOICE = str.maketrans(VOICED, PLAIN)

SPACES = re.compile(r"[\s　]+")

# The families. The strings are what the report prints, so they read as an explanation.
DEVERBAL = "a deverbal noun (連用形) against its verb"
VERB_OF_DEVERBAL = "a verb against its deverbal noun"
INFLECTED = "an inflected form against its plain form"
PLAIN_OF_INFLECTED = "a plain form against its inflected form"
KU_ADVERB = "an adverbial く-form against its adjective"
SURU_COMPOUND = "a する-compound against its noun"
ZURU_JIRU = "a ずる / じる verb pair"
PHRASE_AROUND = "a longer phrase built around the word"
WORD_INSIDE = "the word inside the note's longer phrase"
RENDAKU = "the readings differ only by voicing"
TYPO = "the readings differ by one kana"
WHITESPACE = "the same reading, with stray whitespace"
WIDTH = "the same word in full-width and half-width"
TWO_READINGS = "the same spelling with two real readings"


def devoice(reading: str) -> str:
    """The reading with its voicing marks dropped, so rendaku falls out of a comparison."""
    return reading.translate(DEVOICE)


def kana_distance(a: str, b: str) -> int:
    """Levenshtein distance, to tell a one-kana typo from a different reading."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]


def renyoukei(verb: str) -> set:
    """The 連用形 a verb reading can take, godan and ichidan both."""
    if len(verb) < 2:
        return set()
    stems = set()
    if verb[-1] in TO_I:
        stems.add(verb[:-1] + TO_I[verb[-1]])
    if verb.endswith("る"):
        stems.add(verb[:-1])  # ichidan: 晴れる -> 晴れ
    return stems


def inflections(verb: str) -> set:
    """Every regular form of a verb reading that a note might have been stored under."""
    if len(verb) < 2:
        return set()
    last, stem = verb[-1], verb[:-1]
    forms = set()
    for base in renyoukei(verb):
        forms |= {base + "て", base + "た", base + "ます", base + "ません", base + "ました"}
        forms.add(base + "なさい")
    if verb.endswith("る"):  # ichidan
        forms |= {stem + "ない", stem + "ず", stem + "られる", stem + "られない", stem + "よう"}
    if last in TO_A:  # godan
        negative = stem + TO_A[last]
        forms |= {negative + "ない", negative + "ず", negative + "れる", negative + "れない"}
    if last in TO_E:
        potential = stem + TO_E[last]
        forms |= {potential + "る", potential + "ない", potential + "ません", potential + "ば"}
    if last in ONBIN:
        sokuon, te = ONBIN[last]
        forms |= {
            stem + sokuon + te,
            stem + sokuon + ("だ" if te == "で" else "た"),
            stem + sokuon + te + "いる",
        }
    return forms


def _same_spelling(note_reading: str, link_reading: str):
    """The families for a link that spells like the note but reads differently."""
    if note_reading == link_reading:
        return None  # nothing differs, so there is nothing to explain
    if SPACES.sub("", note_reading) == SPACES.sub("", link_reading):
        return WHITESPACE
    if devoice(note_reading) == devoice(link_reading):
        return RENDAKU
    if kana_distance(note_reading, link_reading) == 1:
        return TYPO
    return TWO_READINGS


def family(note_form: str, note_reading: str, link_form: str, link_reading: str):
    """The family that explains the two readings, or None when nothing does.

    `note_form` is the note's spelling and `link_form` the array element's `dict_form`; both
    readings are hiragana. A link that already agrees with its note has no family: callers
    pass the ones that disagree, and one that does not is told so rather than being given an
    explanation for a difference that is not there.
    """
    if note_form and note_form == link_form:
        return _same_spelling(note_reading, link_reading)
    if unicodedata.normalize("NFKC", note_form) == unicodedata.normalize("NFKC", link_form):
        return WIDTH
    if note_reading == link_reading + "する" or link_reading == note_reading + "する":
        return SURU_COMPOUND
    if note_reading in renyoukei(link_reading):
        return DEVERBAL
    if link_reading in renyoukei(note_reading):
        return VERB_OF_DEVERBAL
    if link_reading.endswith("い") and note_reading == link_reading[:-1] + "く":
        return KU_ADVERB
    if (
        note_reading[-2:] in ("ずる", "じる")
        and link_reading[-2:] in ("ずる", "じる")
        and note_reading[-2:] != link_reading[-2:]
        and note_reading[:-2] == link_reading[:-2]
    ):
        return ZURU_JIRU
    if note_reading in inflections(link_reading):
        return INFLECTED
    if link_reading in inflections(note_reading):
        return PLAIN_OF_INFLECTED
    if note_form and note_form in link_form and len(link_form) > len(note_form):
        return PHRASE_AROUND
    if link_form and link_form in note_form and len(note_form) > len(link_form):
        return WORD_INSIDE
    return None
