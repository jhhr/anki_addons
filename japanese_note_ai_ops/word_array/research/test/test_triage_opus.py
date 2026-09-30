"""The Opus batches' issue files: a subagent that finds an item's own data wrong names it by its
number in the batch, and the ingest must put that on the right note, once."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import triage_opus as to  # noqa: E402


def test_issues_land_on_the_batchs_notes_once(tmp_path, monkeypatch):
    monkeypatch.setattr(to.td, "data_file", lambda name: tmp_path / name)
    batches = tmp_path / "batches"
    batches.mkdir()
    (batches / "batch_0001.keys.json").write_text(json.dumps(
        {"model": "m", "keys": [{"id": 1, "nid": 111, "key": "a"},
                                {"id": 2, "nid": 222, "key": "b"}]}), encoding="utf-8")
    (batches / "batch_0001.issues.json").write_text(json.dumps(
        [{"id": 2, "issue": "read とばす, but the sense is ころす"},
         {"id": 9, "issue": "no such item"},
         {"id": 1, "issue": "odd spelling", "severity": "minor"}]), encoding="utf-8")
    assert to.ingest_issues(batches) == 2
    assert to.ingest_issues(batches) == 0
    rows = [json.loads(line) for line in
            (tmp_path / "note_issues.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(r["nid"], r["severity"]) for r in rows} == {(222, "error"), (111, "minor")}
