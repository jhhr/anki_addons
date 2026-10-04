"""The triage model's feature table: one row per triage note, from what the other scripts wrote.

Not a script. `table()` joins, by note id:

- `words.jsonl` (triage_extract.py): the word's shape, part of speech, and its siblings, with
  each studied sibling's recall today from `recall_now.jsonl` (triage_recall.py): for a new
  sense or reading of a word already studied, how well that word is held;
- `jmdict_features.jsonl` (triage_jmdict.py) and `frequency.jsonl` (triage_frequency.py);
- `opus_features.jsonl` (triage_opus.py), matched to the note by its cached key, so an answer
  given for an older wording of the note is not used;
- `kanji_recall.jsonl` (triage_kanji.py) for the word's kanji, `exposure.jsonl`
  (triage_exposure.py), and `agent_judgements.jsonl` (triage_agents.py) when there is one;
- `family.jsonl` (triage_family.py): the notes the sentence word arrays link above the word (the
  compounds and phrases it is part of) and below it (its parts), and how many of them the user
  has studied or scheduled, with the best recall today among those studied (`recall_now.jsonl`).

Missing values stay NaN, with an `*_missing` column where the absence itself says something (no
JMdict entry, no Opus answer yet). Feature names start with their group (`shape_`, `sib_`,
`jm_`, `freq_`, `op_`, `kanji_`, `exp_`, `fam_`, `agent_`), which is how triage_model.py reports and
drops them by group. The `vocab-frequency` field is not used: its sources vary and disagree.
"""

from __future__ import annotations

import math
import re
from typing import Optional

import triage_data as td
import triage_opus

KANJI_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
KATAKANA_RE = re.compile(r"[ァ-ヺー]")
POS_GROUPS = {
    "noun": ("noun", "pronoun", "number", "counter"),
    "verb": ("verb", "ichidan", "godan", "auxiliary verb"),
    "adj": ("adjective", "adjectival", "i-adjective", "na-adjective"),
    "adv": ("adverb", "conjunction", "interrogative"),
    "expr": ("expression", "idiom", "phrase"),
    "proper": ("proper noun", "name"),
    "affix": ("suffix", "prefix"),
    "grammar": ("particle", "auxiliary", "copula"),
}
JM_MISC_KEPT = ("arch", "obs", "rare", "obsc", "uk", "col", "sl", "vulg", "hon", "hum", "id",
                "yoji", "on-mim", "abbr", "poet", "form", "dated", "hist", "lit")
JLPT_LEVEL = {"N5": 5, "N4": 4, "N3": 3, "N2": 2, "N1": 1, "beyond": 0}
COMPOSITIONAL_LEVEL = {"single-unit": 0, "transparent": 1, "partly": 2, "opaque": 3}
SENSE_LEVEL = {"main": 3, "common": 2, "secondary": 1, "rare": 0}
GROUPS = ("shape", "sib", "jm", "freq", "op", "kanji", "exp", "fam", "agent")
# triage_family's states of a relative that say the user knows it
FAMILY_KNOWN = ("studied", "scheduled")


def pos_group(pos: str) -> str:
    pos = pos.lower()
    for group in ("proper", "expr", "grammar", "affix", "adj", "adv", "verb", "noun"):
        if any(word in pos for word in POS_GROUPS[group]):
            return group
    return "other"


def log_or_nan(value: Optional[float]) -> float:
    return math.log10(value) if value and value > 0 else math.nan


def shape_features(w: dict) -> dict:
    written = w["word"] or w["kanjified"]
    kanji = KANJI_RE.findall(written)
    group = pos_group(w["pos"])
    row = {
        "shape_len": len(written),
        "shape_kanji": len(kanji),
        "shape_kana_only": float(not kanji),
        "shape_katakana": float(bool(KATAKANA_RE.search(written))),
        "shape_kanjified_differs": float(bool(w["kanjified"]) and w["kanjified"] != written),
        "shape_meaning_marker": float(any(m.startswith("m") for m in w["markers"])),
        "shape_reading_marker": float(any(m.startswith("r") or m in ("on", "kun")
                                          for m in w["markers"])),
        "shape_meaning_len": len(w["meaning"]),
    }
    for g in list(POS_GROUPS) + ["other"]:
        row[f"shape_pos_{g}"] = float(group == g)
    return row


def sibling_features(w: dict, recall: dict[int, dict]) -> dict:
    sibs = w["siblings"]
    meaning = [s for s in sibs if s["relation"] == td.MEANING]
    reading = [s for s in sibs if s["relation"] == td.READING]
    spelling = [s for s in sibs if s["relation"] == td.SPELLING]
    studied = [recall[s["nid"]] for s in meaning if s["nid"] in recall]
    row = {
        "sib_meanings": len(meaning),
        "sib_readings": len(reading),
        "sib_spellings": len(spelling),
        "sib_meaning_studied": float(bool(studied)),
        "sib_reading_studied": float(any(s["nid"] in recall for s in reading)),
        "sib_spelling_studied": float(any(s["nid"] in recall for s in spelling)),
        "sib_meaning_untagged": float(any(not s["tagged"] for s in meaning)),
    }
    if studied:
        best = max(studied, key=lambda r: r["r_cal"])
        row.update({
            "sib_best_recall": best["r_cal"],
            "sib_log_stability": log_or_nan(best["s"]),
            "sib_log_interval": log_or_nan(best["ivl"]),
            "sib_lapses": float(best["lapses"]),
            "sib_difficulty": best["d"] if best["d"] is not None else math.nan,
            "sib_log_passes": math.log1p(best["passes"]),
        })
    else:
        row.update({k: math.nan for k in ("sib_best_recall", "sib_log_stability",
                                          "sib_log_interval", "sib_lapses", "sib_difficulty",
                                          "sib_log_passes")})
    return row


def jmdict_features(j: Optional[dict]) -> dict:
    if not j or not j.get("jm_found"):
        row = {"jm_missing": 1.0, "jm_log_nf_rank": math.nan, "jm_nf_missing": 1.0}
        for k in ("news", "ichi", "spec", "gai"):
            row[f"jm_{k}"] = 0.0
        row.update({"jm_common": 0.0, "jm_senses": math.nan, "jm_entries": 0.0,
                    "jm_field": 0.0, "jm_rare_kanji": 0.0})
        for m in JM_MISC_KEPT:
            row[f"jm_{m}"] = 0.0
        return row
    nf = j.get("jm_nf")
    row = {
        "jm_missing": 0.0,
        # nfXX is the XXth 500-word band of a newspaper corpus's ranking: its middle, in log
        "jm_log_nf_rank": math.log10(nf * 500 - 250) if nf else math.nan,
        "jm_nf_missing": float(not nf),
        "jm_common": float(bool(j.get("jm_common"))),
        "jm_senses": float(j.get("jm_senses", 0)),
        "jm_entries": float(j.get("jm_entries", 0)),
        "jm_field": float(bool(j.get("jm_field"))),
        "jm_rare_kanji": float(any(j.get(f"jm_k_{n}") for n in ("rK", "iK", "oK", "sK"))),
    }
    for k in ("news", "ichi", "spec", "gai"):
        row[f"jm_{k}"] = float(j.get(f"jm_{k}", 0))
    for m in JM_MISC_KEPT:
        row[f"jm_{m}"] = float(bool(j.get(f"jm_first_{m}")))
    return row


def frequency_features(f: Optional[dict]) -> dict:
    """`frequency.jsonl` rows carry `freq_<source>_rank` columns; log10 of each, NaN if absent."""
    row: dict = {}
    for key, value in (f or {}).items():
        if key.startswith("freq_") and key.endswith("_rank"):
            row[f"{key[:-5]}_log_rank"] = log_or_nan(value)
    return row


def opus_features(answer: Optional[dict]) -> dict:
    if not answer:
        row = {"op_missing": 1.0}
        for k in ("op_log_vocab_size", "op_jlpt", "op_familiarity", "op_compositional",
                  "op_sense"):
            row[k] = math.nan
        return row
    row = {
        "op_missing": 0.0,
        "op_log_vocab_size": math.log10(answer["learner_vocab_size"]),
        "op_jlpt": float(JLPT_LEVEL.get(answer["jlpt"], 0)),
        "op_familiarity": float(answer["native_familiarity"]),
        "op_compositional": float(COMPOSITIONAL_LEVEL.get(answer["compositional"], 0)),
        "op_sense": float(SENSE_LEVEL[answer["sense_commonness"]])
        if answer["sense_commonness"] in SENSE_LEVEL else math.nan,
        "op_loanword": float(answer["loan_source"] != "none"),
    }
    for value in triage_opus.REGISTERS:
        row[f"op_reg_{value}"] = float(answer["register"] == value)
    for value in triage_opus.ORIGINS:
        row[f"op_origin_{value}"] = float(answer["origin"] == value)
    for value in triage_opus.KINDS:
        row[f"op_kind_{value}"] = float(answer["kind"] == value)
    for value in triage_opus.DOMAINS:
        row[f"op_dom_{value}"] = float(answer["domain"] == value)
    return row


def kanji_features(w: dict, kanji: dict[str, dict]) -> dict:
    written = w["word"] if w["ignore_kanjified"] else (w["kanjified"] or w["word"])
    chars = KANJI_RE.findall(written)
    if not chars:
        return {"kanji_count": 0.0, "kanji_unstudied": 0.0, "kanji_min_recall": math.nan,
                "kanji_mean_recall": math.nan, "kanji_log_rarest": math.nan}
    recalls = []
    unstudied = 0
    ranks = []
    for c in chars:
        k = kanji.get(c)
        if k is None or not k["studied"]:
            unstudied += 1
            recalls.append(0.0)
        else:
            recalls.append(k["r_cal"] if k["r_cal"] is not None else 0.0)
        ranks.append((k or {}).get("freq_rank") or 3000)
    return {
        "kanji_count": float(len(chars)),
        "kanji_unstudied": float(unstudied),
        "kanji_min_recall": min(recalls),
        "kanji_mean_recall": sum(recalls) / len(recalls),
        "kanji_log_rarest": math.log10(max(ranks)),
    }


def exposure_features(e: Optional[dict]) -> dict:
    e = e or {}
    return {
        "exp_log_sentences": math.log1p(e.get("exp_sentences", 0)),
        "exp_log_reviewed": math.log1p(e.get("exp_reviewed", 0)),
        "exp_log_passes": math.log1p(e.get("exp_passes", 0)),
        "exp_best_r": e.get("exp_best_r", 0.0),
        "exp_log_days_since": math.log1p(e["exp_last_days"])
        if e.get("exp_last_days") is not None else math.nan,
        "exp_log_days_first": math.log1p(e["exp_first_days"])
        if e.get("exp_first_days") is not None else math.nan,
    }


def family_features(f: Optional[dict], recall: dict[int, dict]) -> dict:
    """Over the word's parents and over its parts: how many, how many known, and the best
    recall today among them; whether every part is known; how often it is a top-level word."""
    if f is None:
        row = {"fam_missing": 1.0}
        for k in ("fam_log_sentences", "fam_top_share", "fam_parts", "fam_parts_known",
                  "fam_parts_known_share", "fam_parts_all_known", "fam_parts_best_recall",
                  "fam_parents", "fam_parents_known", "fam_parent_known",
                  "fam_parent_best_recall"):
            row[k] = math.nan
        return row
    row = {
        "fam_missing": 0.0,
        "fam_log_sentences": math.log1p(f["sentences"]),
        "fam_top_share": f["top_level"] / f["sentences"] if f["sentences"] else math.nan,
    }
    for name, rels in (("parts", f["parts"]), ("parents", f["parents"])):
        known = [r for r in rels if r["state"] in FAMILY_KNOWN]
        recalls = [recall[r["nid"]]["r_cal"] for r in known if r["nid"] in recall]
        row[f"fam_{name}"] = float(len(rels))
        row[f"fam_{name}_known"] = float(len(known))
        best = max(recalls) if recalls else math.nan
        if name == "parts":
            row["fam_parts_known_share"] = len(known) / len(rels) if rels else math.nan
            row["fam_parts_all_known"] = float(bool(rels) and len(known) == len(rels))
            row["fam_parts_best_recall"] = best
        else:
            row["fam_parent_known"] = float(bool(known))
            row["fam_parent_best_recall"] = best
    return row


def agent_features(a: Optional[dict]) -> dict:
    if not a:
        return {"agent_missing": 1.0, "agent_score": math.nan}
    return {"agent_missing": 0.0, "agent_score": float(a["score"])}


def opus_answers(words: list[dict]) -> dict[int, dict]:
    """Each note's Opus answer whose key matches the note as it reads now."""
    cache = triage_opus.read_cache()
    out = {}
    for w in words:
        key = triage_opus.cache_key(triage_opus.tc.MODEL, triage_opus.item_text(w))
        hit = cache.get(key)
        if hit is not None:
            out[w["nid"]] = hit["answer"]
    return out


# The user's call: a note of a proper noun should not have been matched, and is wrong data
PROPER_NOUN_NOTE = "should not have matched a proper noun"


def proper_noun_nids(words: list[dict], opus: Optional[dict[int, dict]] = None) -> set[int]:
    """The notes Opus reads as a proper noun, real or fictional. Not the part of speech: a
    note tagged Proper Noun is often a common word (花園, ユーロ), and one tagged Noun a name
    (田中, ハワイ)."""
    opus = opus_answers(words) if opus is None else opus
    return {nid for nid, a in opus.items() if str(a.get("kind", "")).startswith("proper-noun")}


def by_nid(name: str) -> dict[int, dict]:
    return {r["nid"]: r for r in td.read_jsonl(td.data_file(name))}


def table():
    """`(DataFrame of features indexed by nid, list of words)`."""
    import pandas as pd

    words = td.read_jsonl(td.data_file("words.jsonl"))
    recall = by_nid("recall_now.jsonl")
    jm = by_nid("jmdict_features.jsonl")
    freq = by_nid("frequency.jsonl")
    exposure = by_nid("exposure.jsonl")
    agents = by_nid("agent_judgements.jsonl")
    family = by_nid("family.jsonl")
    kanji = {r["kanji"]: r for r in td.read_jsonl(td.data_file("kanji_recall.jsonl"))}
    opus = opus_answers(words)
    rows = []
    for w in words:
        row: dict = {"nid": w["nid"]}
        row.update(shape_features(w))
        row.update(sibling_features(w, recall))
        row.update(jmdict_features(jm.get(w["nid"])))
        row.update(frequency_features(freq.get(w["nid"])))
        row.update(opus_features(opus.get(w["nid"])))
        row.update(kanji_features(w, kanji))
        row.update(exposure_features(exposure.get(w["nid"])))
        # No columns at all until triage_family.py has written its file
        row.update(family_features(family.get(w["nid"]), recall) if family else {})
        row.update(agent_features(agents.get(w["nid"])))
        rows.append(row)
    frame = pd.DataFrame(rows).set_index("nid")
    return frame, words
