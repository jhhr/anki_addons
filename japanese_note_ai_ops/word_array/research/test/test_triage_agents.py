"""An agent must never see the user's label for a note it judges: a subagent answers several
batches in one context, so this holds across every batch of an export, not only within one."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import triage_agents as ta  # noqa: E402


def word(nid, base, reading):
    return {"nid": nid, "key": base, "base": base, "word": base, "kanjified": base,
            "reading": reading, "pos": "Noun", "meaning": f"meaning of {base}", "siblings": []}


def pred(nid, p_known, source=None, level=None):
    return {"nid": nid, "p_known": p_known, "labelled": source is not None,
            "label_source": source, "label_level": level}


def test_no_profile_shows_a_note_the_export_judges(tmp_path, monkeypatch):
    monkeypatch.setattr(ta.td, "data_file", lambda name: tmp_path / name)
    words, preds = [], []
    levels = {0: [0, 0], 1: [1, 1], 2: [2, 2], 3: [3, 3]}
    for i in range(400):
        nid = 1000 + i
        # Every word's reading is shared with one other, as 敵 and 仇 share かたき
        words.append(word(nid, f"語{i}", f"よみ{i // 2}"))
        if i < 240:
            preds.append(pred(nid, 0.5, "anki", levels[i % 4]))   # labelled, in the band
        else:
            preds.append(pred(nid, 0.6))                          # unlabelled, in the band
    for name, rows in (("words.jsonl", words), ("predictions.jsonl", preds),
                       ("frequency.jsonl", []), ("judge_queue.jsonl", []),
                       ("hand_labels.jsonl", [])):
        (tmp_path / name).write_text("".join(json.dumps(r) + "\n" for r in rows),
                                     encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["triage_agents.py", "export", "--batch", "20"])
    assert ta.main() == 0
    folder = tmp_path / ta.FOLDER
    judged = {k["nid"] for f in folder.glob("*.keys.json")
              for k in json.loads(f.read_text(encoding="utf-8"))["keys"]}
    by_nid = {w["nid"]: w for w in words}
    judged_forms = {by_nid[n]["reading"] for n in judged if n < 1240}
    assert any(n < 1240 for n in judged), "some labelled notes are judged, to stack on"
    for md in folder.glob("*.md"):
        profile = md.read_text(encoding="utf-8").split("THE LEARNER'S PROFILE")[1]
        profile = profile.split("ITEMS TO JUDGE")[0]
        for n in judged:
            assert f"- {by_nid[n]['word']} (" not in profile, (md.name, n)
        for line in profile.splitlines():
            if line.startswith("- "):
                reading = line.split("(")[1].split(")")[0]
                assert reading not in judged_forms, (md.name, line)
