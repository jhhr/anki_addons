"""Renders one agent prompt per word of the kanjify audit's hand-fix tasks.

`kanjify_audit.py` writes `output/kanjify_audit_tasks.jsonl`: per word, its left-kana (kana +
kanjified) or meaning items. This fills `agent_template.md` for each and writes
`<out>/NN_<class>_<word>.md`, biggest first; a subagent is told to read one file and follow it,
editing notes through `research/kanjify_note.py`. Agents write field values to `<out>/edits/`.

    py -3.10 word_array/research/kanjify_agents/make_prompts.py [--out DIR]
"""

import argparse
import io
import json
import re
import sys
from pathlib import Path

# Japanese output on a Windows console. This script is not under research/, so it can't take
# _bootstrap's utf-8 stdout; the check is what makes it safe when stdout is not a console.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
ADDON_ROOT = HERE.parents[2]
TASKS = ADDON_ROOT / "output" / "kanjify_audit_tasks.jsonl"


def item_line(it: dict) -> str:
    nids = ",".join(map(str, it["nids"])) or "-"
    kanji = f"  [kanjified as {it['kanji']}]" if "kanji" in it else ""
    return f"- nid {nids}  {it['context']}{kanji}"


def task_text(t: dict) -> tuple[str, str]:
    spelled = ", ".join(f"{k} ×{v}" for k, v in t["kanjified"].items())
    if t["class"] == "left-kana":
        return f"left-kana {t['word']}", (
            f"**This word:** {t['word']} ({t['pos']}) is kanjified in some notes ({spelled}) and left"
            f" in kana in others.\n\nDecide from the policy which way this word goes. If the policy"
            f" kanjifies it, kanjify each *kana use* below (skipping false hits and uses the policy"
            f" keeps in kana, such as a て-helper or negation). If the policy keeps it in kana, "
            f"un-kanjify each *kanjified use* instead. Check the other side too: a kanjified use in"
            f" a policy-kana position is un-kanjified, a wrong kanji is respelled.\n\n"
            f"Kana uses ({len(t['items'])}):\n"
            + "\n".join(item_line(i) for i in t["items"])
            + f"\n\nKanjified uses ({len(t['kanjified_items'])}):\n"
            + "\n".join(item_line(i) for i in t["kanjified_items"])
        )
    return f"meaning {t['word']}", (
        f"**This word:** {t['word']} ({t['pos']}) is kanjified with different kanji across the"
        f" notes: {spelled}.\n\nCheck **every** use below: does the kanji fit the meaning in that"
        f" sentence? Respell the ones that don't (both majority and minority spellings can be"
        f" wrong). Also un-kanjify a use the policy keeps in kana. Different kanji are often"
        f" both right (有る/在る, 付く/就く) — change a spelling only when it is wrong in its"
        f" sentence.\n\nUses ({len(t['items'])}):\n" + "\n".join(item_line(i) for i in t["items"])
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ADDON_ROOT / "output" / "kanjify_agent_prompts")
    parser.add_argument("--tasks", type=Path, default=TASKS)
    args = parser.parse_args()
    template = (HERE / "agent_template.md").read_text(encoding="utf-8")
    edits = args.out / "edits"
    edits.mkdir(parents=True, exist_ok=True)
    tasks = [
        json.loads(l) for l in args.tasks.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    tasks.sort(key=lambda t: -(len(t["items"]) + len(t.get("kanjified_items", []))))
    for n, t in enumerate(tasks, 1):
        title, text = task_text(t)
        word = re.sub(r"[^\w]", "", t["word"])
        slug = f"{n:02d}_{t['class']}_{word}"
        prompt = (
            template.replace("{TITLE}", title)
            .replace("{WORD}", t["word"])
            .replace("{POS}", t["pos"])
            .replace("{ITEMS}", text)
            .replace("{SCRATCH}", str(edits))
            .replace("{SLUG}", slug)
        )
        (args.out / f"{slug}.md").write_text(prompt, encoding="utf-8")
        print(f"{len(t['items']) + len(t.get('kanjified_items', [])):4} {slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
