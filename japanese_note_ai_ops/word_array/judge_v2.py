"""Word matching judge v2: every word judged in a request of its own, under its part of speech's rules.

v1, since removed, asked about all the words of a sentence at once, under one set of rules for
every kind of word. Here each word gets its own prompt, so the rules can go into detail for just
its part of speech (`POS_RULES`), and the op sends a note's requests in parallel.
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
# rule_group() splits these further by the word's place in the array
SPLIT_GROUPS = {"noun": ("noun-main", "noun-sub"), "verb": ("verb", "prefix-verb", "suffix-verb")}

INTRO = """You decide whether one word of a Japanese sentence gets a vocabulary note: a flashcard for learning what this word means in this sentence.

The sentence was split into words by fixed rules that err towards too many words, so many of the words are not worth a note; your job is to catch those. A compound or expression and its components are all listed as words and judged separately: "Part of" names the larger word this one is a component of, "Made of" the components of this word. Judge only this word, the one marked with <b> tags in the sentence.

The basic test: imagine the flashcard for this word, in the meaning it has here, showing this sentence as its example. Does the sentence make sense as an example of that word? A card for 手 (hand) showing a sentence with 手紙 (letter) is no example of "hand", so 手 in 手紙 is dontmatch. A card for 母 (mother) or 親 (parent) showing a sentence with 母親 is a fine example of both, so they match."""

POS_RULES = {
    "noun-main": """Rules for nouns, pronouns, proper nouns and numbers that stand as words of their own, not as a component of a larger word:
- match an ordinary noun or pronoun, however common or easy: 人, 事, 時間, 私, 彼, 此れ, 誰.
- match a compound noun whose meaning is more than its components, or that is an established word of its own: 見た目, 場合, 大学生, 手紙, 母親.
- dontmatch a compound noun that means no more than its components put together: 遂行能力 is simply 遂行 + 能力, 日本社会 is 日本 + 社会. Its components keep their notes.
- match a word made of a word plus a suffix, however transparent: 芸術家, 科学者, 王様, 父さん, 詩人, 先週, 硬化.
- dontmatch a word that is only 御 plus a word: 御寺, 御姉さん, 御話. The word and 御 keep their notes. But 御前 (you) is a word of its own: match it.
- dontmatch a pronoun that is only a pronoun plus a plural suffix (彼等, 私達, 奴等, 此奴等): the pronoun keeps the note.
- match a proper noun as a whole, and a four-kanji idiom (yojijukugo).
- match a number standing alone (百, 二十) and a number plus a counter (三つ, 一人, 二人); dontmatch a date or length of time (七月, 一週間, 一年間, 一ヶ月).""",
    "noun-sub": """Rules for nouns, pronouns, proper nouns and numbers that are a component of a larger word ("Part of" names it; that word is judged separately):
- dontmatch a component of a compound whose meaning can't be built from its components' meanings: 手 and 紙 in 手紙, and the components of 名前, 花火, 電話, 学校, 物語, 電車, 会社, 世界, 時間, 大学, 文化. The compound keeps the note. Most two-kanji Sino-Japanese words are like this.
- match a component whose own meaning still shows in the compound: 母 and 親 in 母親, 足 and 音 in 足音, 山 and 道 in 山道.
- match the word inside a word plus a suffix, however transparent: 芸術 in 芸術家, 科学 in 科学者, 週 in 先週, 戦 in 戦後. But dontmatch a piece that is no word by itself, like 硬 in 硬化.
- match the word inside 御 plus a word (寺 in 御寺, 話 in 御話), and the pronoun inside a pronoun plus a plural suffix (私 in 私達, 彼 in 彼等).
- dontmatch the single kanji 此, 其, 彼 or 何 as the first piece of a demonstrative or question word like 其の, 其れ, 此等, 何時, 何故: the whole word keeps the note. A two-kana pronoun like 其れ or 此れ is a word: match it also inside a larger word.
- match a noun that is a component of an idiom or expression: 羽目 in 羽目を外す, 根 in 根に持つ, 物 in 物か, 為 in 為に.
- dontmatch the components of a proper noun and of a four-kanji idiom (yojijukugo): the whole keeps the note.
- dontmatch a component that is not a word of its own in this sentence, like 合 in 場合 or 供 in 子供.
- dontmatch a number inside a number plus a counter (三 in 三つ, 二 in 二度, 七 in 七月); match a date or length of time only as a component of an expression, like 一日 in 一日中.""",
    "verb": """Rules for verbs (given in their dictionary form, whatever form the sentence has):
- match an ordinary verb, however common or easy, 為る (する) included, also where it only makes the noun before it a verb (勉強為る).
- match a verb used as an auxiliary after a て-form: 見る in て見る, 呉れる, 貰う, 置く, 行く, 来る, 下さい.
- match a verb that is a component of an expression or of a noun: 言う in と言う or 然う言う, 有る in で有る, 関する in に関して, 成る in 事に成る.
- match a compound verb whose meaning is more than its components: 登り切る, 見付ける, 取り消す.
- dontmatch a compound verb that means no more than its components put together: 連れて行く is simply 連れる + 行く. Its components keep their notes.
- dontmatch 為る in として and 就く in に就いて (について): they are only part of the particle.""",
    "prefix-verb": """Rules for a verb that is the first of the two verbs a compound verb is made of ("Part of" names the compound verb, which is judged separately): 話す in 話し合う, 飲む in 飲み込む, 立つ in 立ち上がる.
- The test: does this verb, with its own meaning alone, consistently give a specific kind of meaning to the compound verbs it forms as their first part? If it does, the sentence is an example of that meaning: match. If it only goes into the compound as part of a whole with a meaning of its own, or only adds emphasis, dontmatch.
- match a first verb whose own action is still done in the compound: 話す in 話し合う (talk with each other), 飲む in 飲み込む (swallow), 追う in 追い付く (catch up by chasing).
- dontmatch a first verb that is only an emphasising prefix or whose meaning is lost in the compound: 打つ in 打ち明ける, 差す in 差し上げる, 取る in 取り止める.
- match a verb in its て-form before an auxiliary verb (付く in 付いて行く, 為る in 為て遣る) as an ordinary verb.
- Decide case by case: of the two verbs of a compound, both, one or neither can match.""",
    "suffix-verb": """Rules for a verb that is the second of the two verbs a compound verb is made of ("Part of" names the compound verb, which is judged separately): 合う in 話し合う, 込む in 飲み込む, 上がる in 立ち上がる.
- The test: does this verb, with its own meaning alone, consistently give a specific kind of meaning to the compound verbs it forms as their second part? If it does, the sentence is an example of that meaning: match. If it only goes into the compound as part of a whole with a meaning of its own, dontmatch.
- match a second verb that adds a meaning of its own to many compounds: 合う (with each other), 込む (into, thoroughly), 切る (completely), 出す (out, suddenly start), 始める (start), 続ける (keep on), 直す (again), 過ぎる (too much), 上がる (up, finish), 付く in 追い付く (reach).
- dontmatch a second verb whose meaning is lost in the compound, where the compound is a word of its own: 付ける in 見付ける.
- match a verb used as an auxiliary after a て-form (行く in 付いて行く, 遣る in 為て遣る) as an ordinary verb.
- Decide case by case: of the two verbs of a compound, both, one or neither can match.""",
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
- dontmatch a piece that is no prefix, suffix or counter in this sentence, only part of the word it is in, like 合 in 場合 or 御 in 御前 (you).
- apply the basic test: 化 in 硬化 matches (a card for the suffix 化 fits it), 化 in 文化 does not (文化 is a word of its own, not 文 + 化).""",
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


def rule_group(elem: list, parents: list[list]) -> str:
    """The `POS_RULES` key for `elem`: its part of speech's group, split further where the array
    tells the cases apart. Nouns are `noun-main` at the top level and `noun-sub` inside another
    word; a verb that is one of exactly two verb sub-words of a verb is `prefix-verb` or
    `suffix-verb` by its place (買い切る: 買う, 切る)."""
    group = pos_group(elem[1])
    if group == "noun":
        return "noun-sub" if parents else "noun-main"
    if group == "verb" and parents and parents[-1][1] == "verb":
        subs = [s for s in parents[-1][5] if len(s) > 1]
        if len(subs) == 2 and all(s[1] == "verb" for s in subs):
            return "prefix-verb" if subs[0] is elem else "suffix-verb"
    return group


def _describe(elem: list) -> str:
    return f"{elem[2]} [{elem[3]}], {elem[1]}"


def word_prompt(elem: list, sentence: str, parents: list[list], group: str) -> str:
    """The prompt for one word under `group`'s rules: `sentence` has it in `<b>`, `parents` are
    the words it is a component of, outermost first."""
    lines = [f"Sentence: {sentence}", f"Word: {_describe(elem)}"]
    lines += [f"Part of: {_describe(parent)}" for parent in reversed(parents)]
    subs = [s for s in elem[5] if len(s) > 1]
    if subs:
        lines.append("Made of: " + " + ".join(f"{s[2]} [{s[3]}]" for s in subs))
    entry = "\n".join(lines)
    return f"{INTRO}\n\n{POS_RULES[group]}\n\n{entry}\n\n{OUTRO}"


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
            group = rule_group(elem, parents)
            asks.append(WordAsk(elem, group, word_prompt(elem, sentence, parents, group)))
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
