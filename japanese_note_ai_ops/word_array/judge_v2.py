"""Word matching judge v2: every word judged in a request of its own, under its part of speech's rules.

v1 (`match_flags.judge_prompt`) asks about all the words of a sentence at once, under one set of
rules for every kind of word. Here each word gets its own prompt, so the rules can go into detail
for just its part of speech (`POS_RULES`), and the op sends a note's requests in parallel.
Particles and the copula never get a note: they are judged `dontmatch` without asking.

The prompts are a first draft, to be tuned against the judge eval (task 6d-2).
"""

from typing import Any, Iterable, NamedTuple, Optional

from .match_flags import (
    DONT_MATCH,
    JUDGE_NEW,
    MATCH,
    MatchState,
    iter_highlighted,
    match_state,
    set_dont_match,
    set_match,
)

AUTO_DONT_MATCH_POS = frozenset({"particle", "copula"})

REASON_FIELD = "reason"
DECISION_FIELD = "decision"
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        REASON_FIELD: {"type": "string"},
        DECISION_FIELD: {"type": "string", "enum": [MATCH, DONT_MATCH]},
    },
    "required": [REASON_FIELD, DECISION_FIELD],
    "additionalProperties": False,
}

POS_GROUPS = {
    "noun": "noun",
    "proper noun": "noun",
    "pronoun": "noun",
    "number": "noun",
    "verb": "verb",
    "adjective": "adjective",
    "na-adjective": "adjective",
    "adjectival": "adjective",
    "adverb": "adverb",
    "conjunction": "adverb",
    "interjection": "adverb",
    "expression": "expression",
    "prefix": "affix",
    "suffix": "affix",
    "counter": "affix",
    "auxiliary": "auxiliary",
}
OTHER_GROUP = "other"

INTRO = """You decide whether one word of a Japanese sentence gets a vocabulary note: a flashcard for learning what this word means in this sentence.

The sentence was split into words by fixed rules that err towards too many words, so many of the words are not worth a note; your job is to catch those. A compound or expression and its components are all listed as words and judged separately: "Part of" names the larger word this one is a component of, "Made of" the components of this word. Judge only this word, the one marked with <b> tags in the sentence."""

POS_RULES = {
    "noun": """Rules for nouns, pronouns, proper nouns and numbers:
- match an ordinary noun or pronoun, however common or easy: 人, 事, 時間, 私, 此れ, 誰.
- match a compound noun whose meaning is more than its components, or that is an established word of its own: 見た目, 場合, 大学生.
- dontmatch a compound noun that means no more than its components put together: 遂行能力 is simply 遂行 + 能力, 日本社会 is 日本 + 社会. Its components keep their notes.
- match a word made of a word plus a suffix, however transparent: 芸術家, 科学者, 王様, 父さん, 詩人.
- dontmatch a word that is only 御 plus a word: 御寺, 御姉さん, 御話. The word and 御 keep their notes. But 御前 (you) is a word of its own: match it.
- dontmatch a pronoun that is only a pronoun plus a plural suffix (彼等, 私達, 奴等, 此奴等): the pronoun keeps the note.
- dontmatch the single kanji 此, 其, 彼 or 何 as the first piece of a demonstrative or question word like 其の, 其れ, 何時, 何故: the whole word keeps the note. A two-kana pronoun like 其れ or 此れ is a word: match it.
- match a noun that is a component of an idiom or expression: 羽目 in 羽目を外す, 根 in 根に持つ, 物 in 物か.
- match a proper noun as a whole; dontmatch the components of a proper noun.
- dontmatch a component of a four-kanji idiom (yojijukugo): the idiom keeps the note.
- dontmatch a component that is not a word of its own in this sentence, like 合 in 場合 or 供 in 子供.
- match a number standing alone (百, 二十) and a number plus a counter (三つ, 一人, 二人); dontmatch a number inside a number plus a counter (三 in 三つ, 七 in 七月), and a date or length of time (七月, 一週間, 一年間, 一ヶ月).""",
    "verb": """Rules for verbs (given in their dictionary form, whatever form the sentence has):
- match an ordinary verb, however common or easy, 為る (する) included, also where it only makes the noun before it a verb (勉強為る).
- match a verb used as an auxiliary after a て-form: 見る in て見る, 呉れる, 貰う, 置く, 行く, 来る, 下さい.
- match a verb that is a component of a compound verb or of an expression: 合う in 話し合う, 言う in と言う or 然う言う, 有る in で有る, 関する in に関して, 成る in 事に成る.
- match a compound verb whose meaning is more than its components: 登り切る, 見付ける, 取り消す.
- dontmatch a compound verb that means no more than its components put together: 連れて行く is simply 連れる + 行く. Its components keep their notes.
- dontmatch 為る in として: it is only part of the particle.""",
    "adjective": """Rules for adjectives: い-adjectives, な-adjectives and adjectivals (given in their dictionary form, whatever form the sentence has):
- match an ordinary adjective, however common or easy: 多い (also as 多く), 大きい (also as 大きな), 静か.
- match the adjectivals 此の, 其の, 彼の, 何の.
- match 無い (ない) and 様 (よう) also as a component of an expression: 少なく無い, 仕様も無い, ように, のような.
- match a compound adjective whose meaning is more than its components.
- dontmatch a compound that means no more than its components put together.
- dontmatch a component that is not a word of its own in this sentence.""",
    "adverb": """Rules for adverbs, conjunctions and interjections:
- match an ordinary adverb or conjunction, however common or easy: 先ず, 然し, 又, 迚も.
- match an interjection that is a word (はい, 否, 矢張り); dontmatch a bare exclamation sound: あ, ああ, えっ, うっ, おお.
- dontmatch a word that is only another word plus the particle it happens to take here: 此れは, 上に. match one that is a fixed word of its own, or far more common than the bare word: 正に, 共に, 先ずは, 本当に, 確かに, 絶対に.""",
    "expression": """Rules for expressions (multi-word dictionary entries):
- match a fixed expression whose meaning is more than its words, or that is learned as a unit: 鳥肌が立つ, 間も無く, に就いて, かも知れない.
- dontmatch a grammar pattern built from a word the sentence also lists plus particles, the copula or an auxiliary verb: ように, のように, ような, ようになる, ことになる, ことができる, ほうがいい, と言う, じゃない, そうだ, みたいだ, ために, に関して, を通して, において. Its words keep their notes.
- dontmatch an expression that means no more than its words put together: 連れて行く is simply 連れる + 行く, 如何遣って is 如何 + 遣る, 其れから is 其れ + から. Its words keep their notes.
- dontmatch a word plus the particle or copula it happens to take here: 此れは, 上の, 無しに, 今日は, 誰も, 一度も. Its words keep their notes.""",
    "affix": """Rules for prefixes, suffixes and counters:
- match a prefix, suffix or counter that adds a meaning of its own: 御 (お, ご), さん, 達, 等, 性, 本, 回, 年.
- dontmatch the counter つ (三つ) and a prefix like 第 or 大 that only marks an order or size.
- dontmatch a piece that is no prefix, suffix or counter in this sentence, only part of the word it is in, like 合 in 場合 or 御 in 御前 (you).""",
    "auxiliary": """Rules for auxiliary verbs:
- match an auxiliary that adds a meaning to learn: たい (want to), らしい, そうだ, まい, べき, させる, られる.
- dontmatch an ending that only marks politeness or tense: ます, た.""",
    OTHER_GROUP: """Rules:
- match a word worth learning in this sentence.
- dontmatch a piece that is not a word of its own in this sentence, or a compound that means no more than its components put together.""",
}

OUTRO = f"""Return a JSON object: "{REASON_FIELD}", one short sentence on why, then "{DECISION_FIELD}": "{MATCH}" if the word gets a note, "{DONT_MATCH}" if not."""


class WordAsk(NamedTuple):
    elem: list
    group: str
    prompt: str


class JudgePlan(NamedTuple):
    auto: list[list]  # judged dontmatch by part of speech, without asking
    asks: list[WordAsk]


def pos_group(part_of_speech: str) -> str:
    return POS_GROUPS.get(part_of_speech, OTHER_GROUP)


def _describe(elem: list) -> str:
    return f"{elem[2]} [{elem[3]}], {elem[1]}"


def word_prompt(elem: list, sentence: str, parents: list[list]) -> str:
    """The prompt for one word: `sentence` has it in `<b>`, `parents` are the words it is a
    component of, outermost first."""
    lines = [f"Sentence: {sentence}", f"Word: {_describe(elem)}"]
    lines += [f"Part of: {_describe(parent)}" for parent in reversed(parents)]
    subs = [s for s in elem[5] if len(s) > 1]
    if subs:
        lines.append("Made of: " + " + ".join(f"{s[2]} [{s[3]}]" for s in subs))
    entry = "\n".join(lines)
    return f"{INTRO}\n\n{POS_RULES[pos_group(elem[1])]}\n\n{entry}\n\n{OUTRO}"


def plan_judgements(arr: list, states: Iterable[MatchState] = JUDGE_NEW) -> JudgePlan:
    """The words of `arr` in `states`: particles and the copula to judge without asking, and a
    prompt for every other one. Raises ValueError on match_data it doesn't know."""
    wanted = set(states)
    auto: list[list] = []
    asks: list[WordAsk] = []
    stack: list[list] = []
    for depth, elem, sentence in iter_highlighted(arr):
        del stack[depth:]
        parents = list(stack)
        stack.append(elem)
        if match_state(elem) not in wanted:
            continue
        if elem[1] in AUTO_DONT_MATCH_POS:
            auto.append(elem)
        else:
            asks.append(WordAsk(elem, pos_group(elem[1]), word_prompt(elem, sentence, parents)))
    return JudgePlan(auto, asks)


def set_auto(plan: JudgePlan) -> list[int]:
    """Judge the plan's particles and copulas `dontmatch`; returns the note ids that unlinked."""
    unlinked = [set_dont_match(elem) for elem in plan.auto]
    return [note_id for note_id in unlinked if note_id is not None]


def apply_word_response(elem: list, response: Any) -> Optional[int]:
    """Judge `elem` as the response decided; returns the note id a `dontmatch` unlinked. A
    response without a decision raises ValueError and changes nothing."""
    decision = response.get(DECISION_FIELD) if isinstance(response, dict) else None
    if decision == DONT_MATCH:
        return set_dont_match(elem)
    if decision == MATCH:
        set_match(elem)
        return None
    raise ValueError(f"Expected a {DECISION_FIELD!r} of {MATCH!r} or {DONT_MATCH!r}: {response!r}")
