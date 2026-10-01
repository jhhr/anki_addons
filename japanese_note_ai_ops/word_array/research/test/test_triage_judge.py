"""After a refresh, the judging queue and labels follow a note the match op deleted and made anew,
and only when its new note is unmistakable."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import triage_judge as tj  # noqa: E402


def word(nid, w, reading="よみ", meaning="a meaning"):
    return {"nid": nid, "word": w, "kanjified": w, "reading": reading, "meaning": meaning}


def write(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def test_remap_follows_a_recreated_note_and_leaves_an_ambiguous_one(tmp_path, monkeypatch):
    monkeypatch.setattr(tj.td, "data_file", lambda name: tmp_path / name)
    old = [word(1, "残る"), word(2, "消える"), word(3, "二つ"), word(4, "同じ")]
    new = [word(1, "残る"), word(20, "消える", meaning="reworded"),  # 2 made anew as 20
           word(30, "二つ"), word(31, "二つ", meaning="b"),          # 3: one has the meaning
           word(40, "同じ", meaning="x"), word(41, "同じ", meaning="y")]  # 4: neither has it
    write(tmp_path / "old.jsonl", old)
    write(tmp_path / "words.jsonl", new)
    write(tmp_path / tj.QUEUE, [{"nid": n, "kind": "random", "round": 0} for n in (1, 2, 3, 4)])
    write(tmp_path / tj.HAND_LABELS, [{"nid": 2, "label": "learn", "kind": "random",
                                       "round": 0, "t": 5}])
    assert tj.remap(argparse.Namespace(old_words=tmp_path / "old.jsonl")) == 0
    queue = [json.loads(line) for line in (tmp_path / tj.QUEUE).read_text("utf-8").splitlines()]
    labels = [json.loads(line) for line in
              (tmp_path / tj.HAND_LABELS).read_text("utf-8").splitlines()]
    assert [r["nid"] for r in queue] == [1, 20, 30, 4]
    assert labels[0]["nid"] == 20
    assert json.loads((tmp_path / "remap.json").read_text("utf-8")) == {"2": 20, "3": 30}
