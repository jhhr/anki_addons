"""The name lexicon's anchors and lookup, on hand-built morphs (no Sudachi needed)."""

import unittest
from dataclasses import dataclass

from addon_modules import load_ops_module

names = load_ops_module("names", subdir="word_array")

NOUN = ("名詞", "普通名詞", "一般")
PROPER = ("名詞", "固有名詞", "人名")
SUFFIX = ("接尾辞", "名詞的", "一般")
PARTICLE = ("助詞", "格助詞")
NUMBER = ("名詞", "数詞")


@dataclass
class M:
    surface: str
    pos: tuple
    start: int = 0
    end: int = 0


def morphs(*pieces: tuple) -> list[M]:
    """Adjacent morphs from (surface, pos) pairs; a None surface leaves a one-character gap."""
    out, pos = [], 0
    for surface, tag in pieces:
        if surface is None:
            pos += 1
            continue
        out.append(M(surface, tag, pos, pos + len(surface)))
        pos += len(surface)
    return out


# 不甲斐ない里樹さまの侍女たちを阿多さまの侍女たちが
COURT = morphs(
    ("里", NOUN),
    ("樹", SUFFIX),
    ("さま", SUFFIX),
    ("の", PARTICLE),
    ("侍女", NOUN),
    ("たち", SUFFIX),
    ("を", PARTICLE),
    ("阿多", ("名詞", "固有名詞", "地名")),
    ("さま", SUFFIX),
)
HIMARIN = morphs(("ひま", NOUN), ("りん", SUFFIX))


class HonorificTests(unittest.TestCase):
    def test_name_before_honorific(self):
        self.assertEqual(names.honorific_names(COURT), ["里樹", "阿多"])

    def test_stops_at_a_non_name_noun_and_a_number(self):
        self.assertEqual(names.honorific_names(morphs(("侍女", NOUN), ("さん", SUFFIX))), [])
        self.assertEqual(
            names.honorific_names(morphs(("三", NUMBER), ("郎", NOUN), ("さん", SUFFIX))), ["郎"]
        )

    def test_gap_ends_the_name(self):
        gapped = morphs(("山", NOUN), (None, None), ("田", NOUN), ("さん", SUFFIX))
        self.assertEqual(names.honorific_names(gapped), ["田"])


class LexiconTests(unittest.TestCase):
    def test_nickname_needs_two_occurrences_or_a_vocative(self):
        once = names.build_lexicon([("ひまりん", HIMARIN)])
        self.assertNotIn("ひまりん", once)
        twice = names.build_lexicon([("ひまりん", HIMARIN)] * 2)
        self.assertEqual(twice["ひまりん"].sources, {names.NICKNAME})
        addressed = names.build_lexicon(
            [("ひまりん", HIMARIN), ("「ひまりん、それ褒めてないでしょ」", [])]
        )
        self.assertEqual(addressed["ひまりん"].sources, {names.NICKNAME, names.VOCATIVE})

    def test_vocative_alone_names_nothing(self):
        self.assertEqual(names.build_lexicon([("「先輩、待って」", [])]), {})

    def test_sudachi_proper_noun_only_marks_an_anchored_name(self):
        lexicon = names.build_lexicon([("", COURT), ("", morphs(("東京", PROPER)))])
        self.assertEqual(set(lexicon), {"里樹", "阿多"})
        self.assertEqual(lexicon["阿多"].sources, {names.HONORIFIC, names.SUDACHI})
        self.assertEqual(lexicon["里樹"].count, 1)


PREFIX = ("接頭辞", "*", "*")
WORDS = {("娘", "むすめ"), ("梨花", "りか"), ("当時", "とうじ"), ("おじ", "おじ")}


def word(form: str, reading: str) -> bool:
    return (form, reading) in WORDS


def reads(sentence: list, readings: dict):
    """A reader of hand-built morphs: each morph's reading from `readings`, else its surface."""
    return lambda start, end: "".join(
        readings.get(m.surface, m.surface) for m in sentence if start <= m.start and m.end <= end
    )


class PrecisionTests(unittest.TestCase):
    def test_polite_prefix_or_a_lone_prefix_is_no_name(self):
        polite = morphs(("お", PREFIX), ("医者", NOUN), ("さん", SUFFIX))
        self.assertEqual(names.build_lexicon([("", polite)], word=word), {})
        great = morphs(("大", PREFIX), ("当時", NOUN), ("様", SUFFIX))
        rejected: dict = {}
        corpus = [("", great, reads(great, {"当時": "とうじ"}))]
        self.assertEqual(names.build_lexicon(corpus, word=word, rejected=rejected), {})
        self.assertIn("大当時", rejected)

    def test_dictionary_word_before_the_name_is_trimmed(self):
        run = morphs(("当時", NOUN), ("里", NOUN), ("樹", SUFFIX), ("さま", SUFFIX))
        lexicon = names.build_lexicon([("", run, reads(run, {"当時": "とうじ"}))], word=word)
        self.assertEqual(set(lexicon), {"里樹"})

    def test_dictionary_word_needs_most_of_its_uses_anchored(self):
        anchored = morphs(("娘", NOUN), ("さん", SUFFIX))
        bare = morphs(("娘", NOUN), ("が", PARTICLE))
        r = {"娘": "むすめ"}
        corpus = [("", s, reads(s, r)) for s in [anchored, anchored, bare, bare, bare]]
        rejected: dict = {}
        self.assertEqual(names.build_lexicon(corpus, word=word, rejected=rejected), {})
        self.assertEqual(rejected["娘"], "word, anchored 2 of 5")
        corpus = [("", s, reads(s, r)) for s in [anchored, anchored, bare]]
        self.assertIn("娘", names.build_lexicon(corpus, word=word))

    def test_sudachi_proper_noun_at_an_anchor_is_kept(self):
        tagged = morphs(("梨花", PROPER), ("さま", SUFFIX))
        lexicon = names.build_lexicon([("", tagged, reads(tagged, {"梨花": "りか"}))], word=word)
        self.assertIn("梨花", lexicon)

    def test_hiragana_word_is_no_name(self):
        uncle = morphs(("おじ", NOUN), ("さん", SUFFIX))
        self.assertEqual(names.build_lexicon([("", uncle)] * 3, word=word), {})

    def test_nickname_once_when_no_dictionary_word(self):
        self.assertIn("ひまりん", names.build_lexicon([("ひまりん", HIMARIN)], word=word))


class FindNamesTests(unittest.TestCase):
    def test_mention_read_otherwise_is_no_name(self):
        lexicon = {"真": names.NameEntry(1, {names.HONORIFIC}, {"まこと"})}
        sentence = morphs(("真", NOUN), ("は", PARTICLE))
        self.assertEqual(names.find_names(sentence, lexicon, reads(sentence, {"真": "しん"})), [])
        self.assertEqual(
            names.find_names(sentence, lexicon, reads(sentence, {"真": "まこと"})), [(0, 1, "真")]
        )
        self.assertEqual(names.find_names(sentence, lexicon, reads(sentence, {})), [(0, 1, "真")])

    def test_bare_mention_on_morph_boundaries(self):
        bare = morphs(("里", NOUN), ("樹", SUFFIX), ("は", PARTICLE))
        self.assertEqual(names.find_names(bare, {"里樹": None}), [(0, 2, "里樹")])

    def test_not_inside_a_morph(self):
        inside = morphs(("里", NOUN), ("樹木", NOUN))
        self.assertEqual(names.find_names(inside, {"里樹": None}), [])

    def test_longest_name_wins(self):
        lexicon = {"ひま": None, "ひまりん": None}
        self.assertEqual(names.find_names(HIMARIN, lexicon), [(0, 4, "ひまりん")])


if __name__ == "__main__":
    unittest.main()
