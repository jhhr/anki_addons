"""Edits the hand judge makes to a note's sentence field: turning one `<k>` span back into kana.

`<k>` marks words kanjify_sentence turned from kana into kanji, and the frequent slip is a word
that should have stayed kana. Un-kanjifying a span keeps only its furigana readings, dropping
the tags, the kanji, their brackets and the space before each furigana group, while other text
and tags inside (`<b>`) stay: `<k> 此[こ]の</k>` → `この`, `<k> 下[くだ]さい</k>` → `ください`.

Spans are numbered by their `<k>` tags outside the `<i>` context sentences, which is their order
in the context-stripped sentence the page shows. A `<k>` that no `</k>` closes before the next
tag of the kind still takes its number, so the numbers agree with the page, but can't be edited.
"""

import re
from typing import NamedTuple, Optional

TOKEN_RE = re.compile(r"<i>.*?</i>|</?k>", re.DOTALL)
GROUP_RE = re.compile(r" ?(?<![^ \]>])([^ \[\]<>]+?)\[([^\]]*)\]")
TAG_RE = re.compile(r"<[^>]+>")


class NoteEditError(Exception):
    pass


class Span(NamedTuple):
    start: int  # of <k>
    end: int  # after </k>
    content: Optional[str]  # None when unclosed


def k_spans(field: str) -> list[Span]:
    tags = [m for m in TOKEN_RE.finditer(field) if not m[0].startswith("<i>")]
    out = []
    for i, m in enumerate(tags):
        if m[0] != "<k>":
            continue
        close = tags[i + 1] if i + 1 < len(tags) and tags[i + 1][0] == "</k>" else None
        if close is None:
            out.append(Span(m.start(), m.end(), None))
        else:
            out.append(Span(m.start(), close.end(), field[m.end() : close.start()]))
    return out


def as_kana(content: Optional[str]) -> Optional[str]:
    """The span's content with each furigana group as its reading; None without any group."""
    if content is None or "<k>" in content:
        return None
    kana, groups = GROUP_RE.subn(lambda m: m[2], content)
    return kana if groups else None


def shown(text: str) -> str:
    return TAG_RE.sub("", text).replace(" ", "")


def changes(sentence: str) -> list[Optional[tuple[str, str]]]:
    """For each span, what it reads now and after un-kanjifying, as the page's confirmation
    shows it; None for a span that can't be edited."""
    out: list[Optional[tuple[str, str]]] = []
    for span in k_spans(sentence):
        content = span.content
        kana = as_kana(content)
        out.append(None if content is None or kana is None else (shown(content), shown(kana)))
    return out


def _span(field: str, k: int) -> Span:
    spans = k_spans(field)
    if not 0 <= k < len(spans):
        raise NoteEditError(f"The sentence has no <k> span number {k + 1}.")
    return spans[k]


def unkanjify(field: str, k: int) -> str:
    """The field with its `k`-th `<k>` span turned into kana."""
    span = _span(field, k)
    kana = as_kana(span.content)
    if kana is None:
        raise NoteEditError(
            "That <k> span isn't closed or has no furigana to keep: fix it in Anki."
        )
    return field[: span.start] + kana + field[span.end :]


def span_kana(content: str, groups: set[int]) -> str:
    """A `<k>` span with only these furigana groups (by their order in `content`) as kana, tags
    included; a span with no furigana left loses its tags. Kana after a verb that stays kanji
    stays inside its tags: ` 為[し]て 来[き]た` with {1} → `<k> 為[し]てきた</k>`."""
    found = GROUP_RE.findall(content)
    if "<k>" in content or not found or not groups <= set(range(len(found))):
        raise NoteEditError("That <k> span has no such furigana group: fix it in Anki.")
    index = iter(range(len(found)))
    text = GROUP_RE.sub(lambda m: m[2] if next(index) in groups else m[0], content)
    return f"<k>{text}</k>" if len(groups) < len(found) else text


def unkanjify_groups(field: str, k: int, groups: set[int]) -> str:
    """The field with some furigana groups of its `k`-th `<k>` span turned into kana."""
    span = _span(field, k)
    if span.content is None:
        raise NoteEditError("That <k> span isn't closed: fix it in Anki.")
    return field[: span.start] + span_kana(span.content, groups) + field[span.end :]
