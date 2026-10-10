"""Merging a regenerated word array into the one a note already holds.

"Extract words" refuses a note whose word list field already holds an array: generating over it
would throw away what `match_data` holds and no rule can produce again - the judge's verdicts
and the ids of the notes its words are matched to. But a sentence with a mistake in it has to
be fixable. Where the sentence read ` 小[しょう] 枝[えだ]` and the word is 小枝[こえだ], the
furigana is corrected in the sentence field and the array has to follow, and hand-editing the
rows the correction touches is both tedious and easy to get wrong.

Both arrays are the same generator's output over nearly the same sentence, so everything the
correction did not reach comes back identical and a diff of the two says exactly which rows it
did reach. An unchanged row keeps the old element whole - `match_data`, sub-words and all - and
a changed one is taken from the new array, which is what carries the correction.

A row counts as unchanged only if everything the generator decides about it is the same
(`_key`): raw text, part of speech, dictionary form, reading, and the same again for each of
its sub-words. A word the correction made the generator read differently is therefore a changed
word even where its raw text stands, and the new reading wins - keeping the old element for its
`match_data` would leave the field disagreeing with the generator, which is the state this
whole module exists to get out of.

Within a changed region a word usually survives the correction - 小枝 is still there, only its
reading moved - so `match_data` that would otherwise be lost is carried over, in the two steps
the migration found were needed for the same job: the dictionary form and the reading
together, and then the dictionary form alone, which is what carries a word the old array simply
misread (小枝 read しょうえだ). A step only carries where exactly one old and one new element
of the region still hold that key. Two candidates is the case the migration already decided not to
guess at: the judge can judge such a word again and match_words_to_notes can find its note again
from the sentence, and a coin toss here cannot be undone. Whatever is dropped is reported with
the note id it had, for the caller to log - a lost link is the only part of this worth anyone's
attention.
"""

import difflib
from dataclasses import dataclass, field
from typing import Optional

from . import match_flags
from .match_flags import MatchState

# The diff key of a word element, sub-words nested within it.
Key = tuple


@dataclass
class Merge:
    array: list
    unchanged: bool  # the regeneration changed nothing: every row of the diff is `equal`
    kept: int = 0  # top-level elements taken from the old array
    changed: int = 0  # top-level elements taken from the new array
    carried: int = 0  # elements of a changed region that kept their `match_data`
    lost: list[int] = field(default_factory=list)  # note ids no element holds any more


def merge_arrays(old: list, new: list) -> Merge:
    """`new` regenerated over `old`, keeping every row the regeneration did not change.

    The elements of `new` are the merged array's own, with the carried-over `match_data`
    written into them in place, so a caller that needs `new` untouched passes a copy.

    Raises ValueError if the merged array does not reconstruct the same text as `new` - only a
    bug here can cause that, and the check is what keeps such a bug out of the field.
    """
    opcodes = difflib.SequenceMatcher(
        None,
        [_key(elem) for elem in old],
        [_key(elem) for elem in new],
        # Every sentence is full of repeated particles, and above 200 elements autojunk drops
        # from the matching every element appearing in more than 1% of the sequence: exactly
        # the rows that anchor the diff in a long sentence.
        autojunk=False,
    ).get_opcodes()

    result = Merge(array=[], unchanged=all(tag == "equal" for tag, *_ in opcodes))
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            result.array += old[i1:i2]
            result.kept += i2 - i1
        else:
            result.array += _carry(old[i1:i2], new[j1:j2], result)
            result.changed += j2 - j1

    if _raw(result.array) != _raw(new):
        raise ValueError("the merged array does not reconstruct the sentence")
    return result


def _key(elem: list) -> Key:
    """What the generator decides about a word element, and nothing the judge does."""
    if len(elem) < 6:
        # A tag or a piece of punctuation: ["<k>"], ["。"]
        return (elem[0],)
    return (elem[0], elem[1], elem[2], elem[3], tuple(_key(sub) for sub in elem[5]))


def _raw(arr: list) -> str:
    return "".join(elem[0] for elem in arr)


def _form_and_reading(elem: list) -> Key:
    return (elem[2], elem[3])


def _form(elem: list) -> Key:
    return (elem[2],)


def _carry(old_span: list, new_span: list, result: Merge) -> list:
    """The new elements of a changed region, holding the `match_data` of the old ones they
    still are. Counts what was carried and collects the note ids of what was not."""
    old_words = [elem for _depth, elem in match_flags.iter_words(old_span)]
    new_words = [elem for _depth, elem in match_flags.iter_words(new_span)]
    taken_old: set[int] = set()
    taken_new: set[int] = set()

    for key_of in (_form_and_reading, _form):
        _carry_step(old_words, new_words, taken_old, taken_new, key_of, result)

    for old_elem in old_words:
        note_id = match_flags.matched_note_id(old_elem)
        if note_id is not None and id(old_elem) not in taken_old:
            result.lost.append(note_id)
    return new_span


def _carry_step(
    old_words: list,
    new_words: list,
    taken_old: set[int],
    taken_new: set[int],
    key_of,
    result: Merge,
) -> None:
    """One step of the carry, over what the steps before it left."""
    olds = _unique(old_words, taken_old, key_of)
    news = _unique(new_words, taken_new, key_of)
    for key, old_elem in olds.items():
        new_elem = news.get(key)
        if old_elem is None or new_elem is None:
            # Several elements of the region hold the key on one side or the other, so which
            # one this is cannot be told apart - left to the judge rather than guessed at.
            continue
        if match_flags.match_state(old_elem) is MatchState.UNJUDGED:
            # Nothing to carry, and nothing is claimed: a later step may still want either of
            # these two for something that does.
            continue
        new_elem[4] = list(old_elem[4])
        taken_old.add(id(old_elem))
        taken_new.add(id(new_elem))
        result.carried += 1


def _unique(words: list, taken: set[int], key_of) -> dict[Key, Optional[list]]:
    """key -> the one element of the region holding it, or None where several do. An element a
    step before this one has already spoken for is not in the running at all."""
    out: dict[Key, Optional[list]] = {}
    for elem in words:
        if id(elem) in taken:
            continue
        key = key_of(elem)
        out[key] = None if key in out else elem
    return out
