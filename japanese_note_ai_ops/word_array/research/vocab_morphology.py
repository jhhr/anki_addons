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

Before any family is named, a pair that is not a disagreement at all is turned away. A judging
pass over 72 of these found 14 that no side was wrong in: the two readings were one reading in
two notations, or the element's reading slot held something that is not a reading, or the
element was a piece of a compound whose voicing says nothing about the free-standing word.
Those get `NOT_A_QUESTION`, which is not the same answer as `None`: `None` means no family
describes the difference, and a caller may still act on the link for other reasons, while
`NOT_A_QUESTION` means there is nothing here to act on at all.

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
KANA = re.compile(r"[ぁ-んァ-ヶ]")
# A spelling that is nothing but kana, which therefore transliterates to exactly one reading.
KANA_ONLY = re.compile(r"^[ぁ-んァ-ヶーｰ・\s　]+$")

# The tokenizer's own part-of-speech names, which it leaves in the reading slot of an element it
# could not read: 52 elements across the collection hold one. The spelling each is really the
# word for is kept so a note that genuinely is 記号[きごう] is not mistaken for damage.
POS_LABELS = {
    "きごう": "記号",
    "めいし": "名詞",
    "どうし": "動詞",
    "けいようし": "形容詞",
    "ふくし": "副詞",
    "じょし": "助詞",
    "じょどうし": "助動詞",
}

# The vowel each kana is read with, for folding a long vowel written two ways into one.
VOWEL_OF = {
    kana: vowel
    for vowel, row in (
        ("あ", "あかがさざただなはばぱまやらわゃゎ"),
        ("い", "いきぎしじちぢにひびぴみり"),
        ("う", "うくぐすずつづぬふぶぷむゆるゅゔ"),
        ("え", "えけげせぜてでねへべぺめれ"),
        ("お", "おこごそぞとどのほぼぽもよろをょ"),
    )
    for kana in row
}

# Spellings a dictionary attests two readings for, with one meaning: neither side is a mistake,
# so the pair is not a question. Each was looked up in the MDX dictionaries under `user_files`
# before being put here - 笹竹 is headed ささたけ by 大辞泉・大辞林・広辞苑 and ささだけ by
# 明鏡・新明解, and はじっこ and きょうし are named inside the entry for the other reading.
ATTESTED_VARIANTS = {
    "笹竹": {"ささたけ", "ささだけ"},
    "端っこ": {"はしっこ", "はじっこ"},
    "教示": {"きょうじ", "きょうし"},
}

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
NOT_A_QUESTION = "the two readings are not a disagreement about the word"


def devoice(reading: str) -> str:
    """The reading with its voicing marks dropped, so rendaku falls out of a comparison."""
    return reading.translate(DEVOICE)


def fold_long_vowels(reading: str) -> str:
    """The reading with every long vowel written the one way, as 長音符.

    てめえ and てめー are one reading in two notations, and so are こう and こー. Folding both
    sides lets them compare equal without either notation being called the right one. The fold
    is only ever used to answer "is this the same reading", never to rewrite a reading: turning
    a `TWO_READINGS` pair into a `RENDAKU` one would hand it to the repair, which writes.
    """
    folded = ""
    for kana in reading:
        vowel = VOWEL_OF.get(folded[-1]) if folded else None
        if vowel and (kana == vowel or (vowel in ("え", "お") and kana == "う")):
            folded += "ー"
        elif folded and kana == "ー":
            folded += "ー"
        else:
            folded += kana
    return folded


def same_reading(a: str, b: str, fold: bool = True) -> bool:
    """Whether two readings are the same reading, however each writes its long vowels.

    `fold` is the caller's answer to "does anything here decide which notation is right": a kana
    spelling does, so the two notations are then a difference worth repairing rather than one
    reading written twice.
    """
    if not a:
        return False
    return a == b or (fold and fold_long_vowels(a) == fold_long_vowels(b))


def not_a_reading(form: str, reading: str) -> bool:
    """Whether an element's reading slot holds something that is not a reading at all.

    Such an element is not a rival reading and never evidence about the note's: the slot holds
    the element's own Latin text echoed back (OL, DVD), the tokenizer's part-of-speech name, or
    a reading cut off mid-mora. A pair built on one is a question with no answer, not a hard one.
    """
    if not reading:
        return True
    if not KANA.search(reading):
        # OL[OL], DVD[DVD]: the raw text copied into the slot, and a reading that is all kanji.
        return True
    if reading in POS_LABELS and form not in (reading, POS_LABELS[reading]):
        return True
    if reading.endswith(("っ", "ッ")):
        # 蹄鉄[ていてっ]: no Japanese word's reading ends in a bare sokuon, so it is a truncation.
        return True
    return False


def doubled_reading(note_reading: str, link_reading: str) -> bool:
    """Whether the link's reading is the note's with a piece of itself glued on the front.

    浮かない顔[うかないうかないかお]: the array builder joined the parent element's kana prefix
    to a child whose `raw_text` already carried the whole expression's furigana. The result is
    never a competing reading, only the same one said twice.
    """
    if len(link_reading) <= len(note_reading) or not link_reading.endswith(note_reading):
        return False
    return note_reading.startswith(link_reading[: -len(note_reading)])


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


def _same_spelling(note_reading: str, link_reading: str, fold: bool = True):
    """The families for a link that spells like the note but reads differently."""
    if same_reading(note_reading, link_reading, fold):
        return None  # nothing differs, so there is nothing to explain
    if SPACES.sub("", note_reading) == SPACES.sub("", link_reading):
        return WHITESPACE
    if devoice(note_reading) == devoice(link_reading):
        return RENDAKU
    if kana_distance(note_reading, link_reading) == 1:
        return TYPO
    return TWO_READINGS


def family(note_form: str, note_reading: str, link_form: str, link_reading: str, depth: int = 0):
    """The family that explains the two readings, or None when nothing does.

    `note_form` is the note's spelling and `link_form` the array element's `dict_form`; both
    readings are hiragana. `depth` is the element's nesting in the array, 0 for a word the
    sentence holds on its own and 1 or more for a piece of a compound.

    A link that already agrees with its note gets `NOT_A_QUESTION` rather than an explanation
    for a difference that is not there, and so does one whose difference is not a disagreement
    about the word: see the module docstring for why that is not the same answer as `None`.
    """
    # A kana spelling transliterates to exactly one reading, so which notation a long vowel is
    # written in is decidable there and worth repairing (コーラス reads こーらす, not こうらす).
    # For every other spelling neither notation is the right one, so the two are one reading.
    decidable = bool(KANA_ONLY.match(note_form or ""))
    if same_reading(note_reading, link_reading, fold=not decidable):
        # Identical readings reached WIDTH below whenever the spellings differed only by width,
        # which sent ＰＫ[ぴーけー] against PK[ぴーけー] to the repair as a reading to fix.
        return NOT_A_QUESTION
    if not_a_reading(link_form, link_reading):
        return NOT_A_QUESTION
    if doubled_reading(note_reading, link_reading):
        return NOT_A_QUESTION
    if depth > 0 and not KANA.search(link_form) and devoice(note_reading) == devoice(link_reading):
        # 髪型 holds 型[がた] and 膝頭 holds 頭[がしら]: compound-internal rendaku of the very
        # reading the note stores. Inside a compound the voicing is expected and says nothing
        # about how the word is read on its own, so there is no disagreement to settle.
        #
        # Only a form with no okurigana, though. 染みる[じみる] sits at the same depth with the
        # same voicing, but the okurigana makes it an inflecting word rather than a bound
        # morpheme - and じみる really is a suffix of its own, which is a `split` to act on and
        # not a pair to wave through.
        return NOT_A_QUESTION
    if ATTESTED_VARIANTS.get(link_form, set()) >= {note_reading, link_reading}:
        return NOT_A_QUESTION
    if note_form and note_form == link_form:
        return _same_spelling(note_reading, link_reading, fold=not decidable)
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
