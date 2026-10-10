"""Load the hand-made gold word arrays from gold_examples.md and check their structure.

gold_examples.md is a copy of the design notes' examples file; pass another path to use a
newer version. Small JSON slips (trailing commas, a comma missing before an array) are repaired
leniently so one typo doesn't hide the whole example; validate() reports structural problems.
"""

import json
import re
from pathlib import Path
from typing import Optional

GOLD_MD = Path(__file__).resolve().parent / "gold_examples.md"

FURI_RE = re.compile(r" ?([^ >\[\]]+?)\[([^\]]*)\]")
TAG_RE = re.compile(r"<[^>]+>")


def strip_to_plain(raw: str) -> str:
    """Remove html tags, furigana and the furigana separator spaces."""
    return FURI_RE.sub(r"\1", TAG_RE.sub("", raw)).replace(" ", "")


def _lenient_json(block: str):
    fixed = re.sub(r'"\s*\n(\s*)\[', '",\n\\1[', block)  # missing comma before an array
    fixed = re.sub(r",(\s*[\]}])", r"\1", fixed)  # trailing commas
    return json.loads(fixed)


def _normalize(elem: list) -> list:
    """Wrap a sub_words list that was written flat into its parent."""
    if len(elem) > 6:
        elem = elem[:5] + [elem[5:]]
    if len(elem) >= 6:
        elem = elem[:5] + [[_normalize(s) for s in elem[5]]]
    return elem


def load(path: Optional[Path] = None) -> dict[int, tuple[str, list]]:
    """{example number: (sentence, gold array)}"""
    md = (path or GOLD_MD).read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(
        r"^Example sentence (\d+): ?(.*?)\n```(.*?)```json\s*\n(.*?)```", md, flags=re.M | re.S
    ):
        arr = [_normalize(e) for e in _lenient_json(m.group(4))]
        out[int(m.group(1))] = (m.group(2), arr)
    return out


def concat_raw(arr: list) -> str:
    return "".join(e[0] for e in arr)


def iter_words(arr: list, depth: int = 0):
    """(depth, element) for every word element (more than one field), sub_words included."""
    for e in arr:
        if len(e) > 1:
            yield depth, e
            if len(e) >= 6 and e[5]:
                yield from iter_words(e[5], depth + 1)


def validate(sentence: str, arr: list) -> list[str]:
    """Structural problems: raw_text not reconstructing the sentence, missing fields, sub-words
    whose text doesn't make up their parent's."""
    issues = []
    want = re.sub(r"</?b>", "", sentence)
    if concat_raw(arr) != want:
        issues.append(
            f"raw_text concat != sentence\n    want {want!r}\n    got  {concat_raw(arr)!r}"
        )
    for _, e in iter_words(arr):
        if len(e) != 6:
            issues.append(f"{len(e)} fields: {e}")
        elif e[5] and strip_to_plain(concat_raw(e[5])) != strip_to_plain(e[0]):
            issues.append(f"sub_words text != parent: {e[0]!r} vs {concat_raw(e[5])!r}")
    return issues


if __name__ == "__main__":
    import io
    import sys

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    examples = load(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(f"{len(examples)} examples")
    for num, (sentence, arr) in sorted(examples.items()):
        for issue in validate(sentence, arr):
            print(f"ex{num}: {issue}")
