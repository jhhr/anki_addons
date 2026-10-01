"""Which benchmark summaries benchmark_compare puts on each side: the head's, and a base that is
none of them."""

from __future__ import annotations

import json

import benchmark_compare


def summary(label: str, commit: str, seconds: float) -> dict:
    return {
        "label": label,
        "where": {"commit": commit},
        "fixture": "corpus",
        "profile": {"scale": 0.02},
        "corpus": {"inputs_sha1": "same"},
        "runs": [{"seconds": seconds}],
    }


# A history: main, three runs of an experiment, then a later commit
HISTORY = [
    summary("main", "aaaa111", 100.0),
    summary("exp", "bbbb222", 201.0),
    summary("exp", "bbbb222", 202.0),
    summary("exp", "bbbb222", 203.0),
    summary("later", "cccc333", 300.0),
]


def labels(group: list[dict]) -> list[str]:
    return [s["label"] for s in group]


def test_a_head_of_several_summaries_is_compared_with_the_one_before_them():
    # The base was another exp summary: the head against itself
    heads, bases = benchmark_compare.select(HISTORY[:4], "exp", None)

    assert labels(heads) == ["exp", "exp", "exp"]
    assert labels(bases) == ["main"]


def test_a_head_earlier_in_the_history_is_compared_with_what_came_before_it():
    # The base was the last summary in the file, recorded after the head: every sign turned
    heads, bases = benchmark_compare.select(HISTORY, "exp", None)
    assert labels(bases) == ["main"]

    heads, bases = benchmark_compare.select(HISTORY, "aaaa", None)
    assert labels(heads) == ["main"] and bases == []


def test_the_defaults_are_the_last_summary_and_the_one_before_it():
    heads, bases = benchmark_compare.select(HISTORY, None, None)

    assert labels(heads) == ["later"]
    assert [s["runs"][0]["seconds"] for s in bases] == [203.0]


def test_a_base_selector_never_takes_a_head_summary():
    heads, bases = benchmark_compare.select(HISTORY, "exp", "bbbb")

    assert len(heads) == 3 and bases == []


def test_a_summary_of_another_profile_is_on_neither_side():
    other = {**summary("main", "aaaa111", 50.0), "profile": {"scale": 1.0}}

    heads, bases = benchmark_compare.select([other, *HISTORY[:4]], "exp", None)

    assert [s["runs"][0]["seconds"] for s in bases] == [100.0]


def test_the_command_prints_the_two_sides_it_compares(tmp_path, capsys):
    history = tmp_path / "corpus.jsonl"
    history.write_text("".join(json.dumps(s) + "\n" for s in HISTORY), encoding="utf-8")

    assert benchmark_compare.main([str(history), "--head", "exp"]) == 0

    out = capsys.readouterr().out
    assert "base: aaaa111 (main), 1 runs" in out
    assert "head: bbbb222 (exp), 3 runs" in out
