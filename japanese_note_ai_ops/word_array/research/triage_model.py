"""The triage model: how sure the user would be that they know each word, fitted on their labels.

    py -3.10 word_array/research/triage_model.py [--no-hgb] [--judged-date]

The labels are one ordered scale, the user's certainty (triage_labels.py): learn < schedule
short (an interval under `--long-days`) < schedule long < suspend. They come from two places:
the notes judged in Anki (`labels.jsonl`), and the notes judged on triage_judge.py's page
(`hand_labels.jsonl`). A hand label of Schedule carries no interval, so it says only that the
level is short or long: each label is a range of levels, one level wide except there, and the
models are fitted on the probability of the range (interval censoring).

The fixed random 100 of the judging page (kind `random` in `judge_queue.jsonl`) are never
trained on: they are an honest sample of the notes the model acts on, and the model's accuracy
on them, as judged so far, is reported for each model.

Two models of the scale:

- an ordinal logistic regression (cumulative logit, L2, its strength picked by cross-validation)
  over standardized features, missing values set to the median with an `_isnan` column beside
  them: interpretable, its coefficients are what carries it;
- small gradient-boosted trees, one binary model per step of the scale (level >= 1, >= 2, >= 3,
  Frank and Hall's ordinal trees), for what the linear model cannot bend to. A censored label
  that spans a step (a hand Schedule, at the short/long step) is left out of that step's model.

Both are scored by repeated stratified 5-fold cross-validation: AUC at each step of the scale
(known at all, i.e. not learn; long or suspend; suspend), the log loss of the label's range, and
Spearman's rho between the expected level and the label's midpoint. Which features carry it: the
ordinal model's standardized coefficients, the trees' permutation importance, and the loss when
a whole group (triage_features.GROUPS) is left out. `--judged-date` adds when each word was
judged in Anki, to see whether the user's standard moved over time once the words themselves are
accounted for; suspended cards are left out of it, since their date is only their last change.

The model that cross-validates better (or their average, when that is better still) scores
every note: out-of-fold for the labelled ones, fitted on all labels for the rest. When
triage_agents.py has answered some notes, a second stage stacks its P(known) on the first
stage's out-of-fold output, fitted on the labelled notes that have both, and is used for the
notes it answered if it cross-validates better there. Writes `predictions.jsonl` (per note: the
four levels' probabilities, P(known), the expected level, the label if any, and the features
pushing it most either way) and `reports/model.txt`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from typing import Any, Optional

import numpy as np

import triage_data as td
import triage_features as tf

LEVELS = ("learn", "schedule_short", "schedule_long", "suspend")
LONG_DAYS = 900
# The trial's best was its largest, 30: the grid reaches well past it
L2_GRID = (3.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0)
QUEUE = "judge_queue.jsonl"
HAND_LABELS = "hand_labels.jsonl"
AGENTS = "agent_judgements.jsonl"
STACK_L2 = 1.0


def level_of(label: dict, long_days: int) -> int:
    if label["label"] == "learn":
        return 0
    if label["label"] == "suspend":
        return 3
    return 2 if label["interval"] >= long_days else 1


def bounds_of(label: dict, long_days: int) -> tuple[int, int]:
    """The range of levels a label allows: one level, or short-or-long for a Schedule with no
    interval (a hand label)."""
    if label["label"] == "schedule" and label.get("interval") is None:
        return 1, 2
    level = level_of(label, long_days)
    return level, level


class Labels:
    """Every label by note id as a level range, with where it came from."""

    def __init__(self, long_days: int) -> None:
        self.anki = {r["nid"]: r for r in td.read_jsonl(td.data_file("labels.jsonl"))}
        self.holdout = {r["nid"] for r in td.read_jsonl(td.data_file(QUEUE))
                        if r["kind"] == "random"}
        self.hand = {r["nid"]: r for r in td.read_jsonl(td.data_file(HAND_LABELS))
                     if r["label"] in ("suspend", "schedule", "learn")}
        self.bounds: dict[int, tuple[int, int]] = {}
        self.source: dict[int, str] = {}
        for nid, r in self.anki.items():
            self.bounds[nid] = bounds_of(r, long_days)
            self.source[nid] = "anki"
        for nid, r in self.hand.items():
            if nid in self.anki:
                continue
            self.bounds[nid] = bounds_of(r, long_days)
            self.source[nid] = "holdout" if nid in self.holdout else "hand"

    def training(self, index) -> list[int]:
        return [nid for nid in index if self.source.get(nid) in ("anki", "hand")]

    def held_out(self, index) -> list[int]:
        return [nid for nid in index if self.source.get(nid) == "holdout"]

    def arrays(self, nids: list[int]):
        lo = np.array([self.bounds[n][0] for n in nids], dtype=int)
        hi = np.array([self.bounds[n][1] for n in nids], dtype=int)
        return lo, hi


# --- the ordinal logistic model ----------------------------------------------------------


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def range_prob(probs, lo, hi):
    """P(lo <= level <= hi) of each row."""
    cum = np.cumsum(probs, axis=1)
    below = np.where(lo > 0, cum[np.arange(len(lo)), np.maximum(lo - 1, 0)], 0.0)
    return np.clip(cum[np.arange(len(hi)), hi] - below, 1e-12, 1)


class OrdinalLogit:
    """P(level >= k) = sigmoid(x.w - theta_k), k = 1..K-1, thetas increasing; L2 on w. Each
    label is a range of levels [lo, hi]; its likelihood is P(lo <= level <= hi)."""

    def __init__(self, l2: float = 1.0, levels: int = 4) -> None:
        self.l2 = l2
        self.k = levels

    def _unpack(self, params):
        p = self.p
        w = params[:p]
        thetas = params[p] + np.concatenate([[0.0], np.cumsum(np.exp(params[p + 1:]))])
        return w, thetas

    def fit(self, x, lo, hi=None, sample_weight=None):
        from scipy.optimize import minimize

        hi = lo if hi is None else hi
        n, self.p = x.shape
        steps = self.k - 1
        sw = np.ones(n) if sample_weight is None else sample_weight
        # upper = P(level >= lo) is cum[lo-1] (1 when lo is 0); lower = P(level >= hi+1) is
        # cum[hi] (0 when hi is the top level)
        up = np.zeros((n, steps))
        up[lo > 0, lo[lo > 0] - 1] = 1
        down = np.zeros((n, steps))
        down[hi < steps, hi[hi < steps]] = 1
        at_bottom = (lo == 0).astype(float)

        def loss(params):
            w, thetas = self._unpack(params)
            z = x @ w
            cum = _sigmoid(z[:, None] - thetas[None, :])  # P(y >= k), k = 1..K-1
            prob = np.clip((cum * up).sum(1) + at_bottom - (cum * down).sum(1), 1e-12, 1)
            nll = -(sw * np.log(prob)).sum()
            dens = cum * (1 - cum)  # d cum / d z, and minus d cum / d theta
            dz = -sw * ((dens * up).sum(1) - (dens * down).sum(1)) / prob
            gw = x.T @ dz + self.l2 * w
            gtheta = ((sw / prob)[:, None] * dens * (up - down)).sum(0)
            g0 = gtheta.sum()
            gdelta = np.array([gtheta[i:].sum() * math.exp(params[self.p + i])
                               for i in range(1, steps)])
            return nll + 0.5 * self.l2 * (w @ w), np.concatenate([gw, [g0], gdelta])

        init = np.concatenate([np.zeros(self.p), [-1.0], np.zeros(steps - 1)])
        result = minimize(loss, init, jac=True, method="L-BFGS-B", options={"maxiter": 2000})
        self.params = result.x
        self.coef_, self.thetas_ = self._unpack(result.x)
        return self

    def predict_proba(self, x):
        z = x @ self.coef_
        cum = _sigmoid(z[:, None] - self.thetas_[None, :])
        upper = np.hstack([np.ones((len(x), 1)), cum])
        lower = np.hstack([cum, np.zeros((len(x), 1))])
        return np.clip(upper - lower, 1e-9, 1)


class Prepared:
    """Median imputation with `_isnan` columns, then standardization, learned on a fit set."""

    def fit(self, frame):
        self.columns = list(frame.columns)
        self.nan_cols = [c for c in self.columns if frame[c].isna().any()]
        self.medians = frame.median(numeric_only=True).fillna(0.0)
        x = self._raw(frame)
        self.mean = x.mean(0)
        self.std = x.std(0)
        self.std[self.std < 1e-9] = 1.0
        return self

    def _raw(self, frame):
        filled = frame[self.columns].fillna(self.medians)
        extra = frame[self.nan_cols].isna().astype(float).to_numpy()
        return np.hstack([filled.to_numpy(dtype=float), extra])

    @property
    def names(self) -> list[str]:
        return self.columns + [c + "_isnan" for c in self.nan_cols]

    def transform(self, frame):
        return (self._raw(frame) - self.mean) / self.std


# --- the trees -----------------------------------------------------------------------------


class OrdinalTrees:
    """Frank and Hall: one gradient-boosted binary model per step, P(level >= k). A label whose
    range spans a step says nothing about it and is left out of that step's model."""

    def __init__(self, levels: int = 4, seed: int = 0) -> None:
        self.k = levels
        self.seed = seed

    def fit(self, x, lo, hi=None):
        from sklearn.ensemble import HistGradientBoostingClassifier

        hi = lo if hi is None else hi
        self.models = []
        for step in range(1, self.k):
            known = ~((lo < step) & (step <= hi))
            m = HistGradientBoostingClassifier(
                max_depth=3, learning_rate=0.05, max_iter=250, l2_regularization=1.0,
                min_samples_leaf=20, random_state=self.seed)
            m.fit(x[known], (lo[known] >= step).astype(int))
            self.models.append(m)
        return self

    def predict_proba(self, x):
        cum = np.column_stack([m.predict_proba(x)[:, 1] for m in self.models])
        # the steps are fitted apart, so they may cross; keep them ordered
        cum = np.minimum.accumulate(cum, axis=1)
        upper = np.hstack([np.ones((len(x), 1)), cum])
        lower = np.hstack([cum, np.zeros((len(x), 1))])
        return np.clip(upper - lower, 1e-9, 1)


# --- scoring -------------------------------------------------------------------------------


def scores(lo, hi, probs) -> dict:
    from scipy.stats import spearmanr
    from sklearn.metrics import roc_auc_score

    out = {}
    for step, name in ((1, "auc_known"), (2, "auc_long"), (3, "auc_suspend")):
        known = ~((lo < step) & (step <= hi))
        truth = (lo[known] >= step).astype(int)
        if 0 < truth.sum() < len(truth):
            out[name] = roc_auc_score(truth, probs[known][:, step:].sum(1))
    out["log_loss"] = float(-np.mean(np.log(range_prob(probs, lo, hi))))
    expected = probs @ np.arange(probs.shape[1])
    if len(set((lo + hi).tolist())) > 1:
        out["spearman"] = float(spearmanr(expected, (lo + hi) / 2).correlation)
    return out


def cross_validate(make, frame, lo, hi, repeats: int = 2, seed: int = 0, prepared: bool = True,
                   levels: int = 4):
    """Out-of-fold probabilities of the first repeat, and the mean scores over all."""
    from sklearn.model_selection import StratifiedKFold

    strata = lo * 4 + hi
    # A range seen fewer times than there are folds (the first hand Schedules, short or long)
    # cannot be split five ways: it is stratified with its lowest level instead
    counts = {k: int((strata == k).sum()) for k in set(strata.tolist())}
    rare = np.array([counts[k] < 5 for k in strata.tolist()])
    strata = np.where(rare, lo * 4 + lo, strata)
    runs, first = [], None
    for r in range(repeats):
        oof = np.zeros((len(lo), levels))
        folds = StratifiedKFold(5, shuffle=True, random_state=seed + r).split(frame, strata)
        for train, test in folds:
            if prepared:
                prep = Prepared().fit(frame.iloc[train])
                xtr, xte = prep.transform(frame.iloc[train]), prep.transform(frame.iloc[test])
            else:
                xtr, xte = frame.iloc[train].to_numpy(float), frame.iloc[test].to_numpy(float)
            oof[test] = make().fit(xtr, lo[train], hi[train]).predict_proba(xte)
        runs.append(scores(lo, hi, oof))
        if first is None:
            first = oof
    mean = {k: float(np.mean([s[k] for s in runs if k in s])) for k in runs[0]}
    return first, mean


def fmt(s: dict) -> str:
    return ", ".join(f"{k} {v:.3f}" for k, v in s.items())


ACTIONS = ("learn", "schedule", "suspend")


def action_probs(probs):
    """The four levels folded into the three actions."""
    return np.column_stack([probs[:, 0], probs[:, 1] + probs[:, 2], probs[:, 3]])


def action_of_bounds(lo, hi) -> np.ndarray:
    return np.where(lo == 0, 0, np.where(lo == 3, 2, 1))


def holdout_lines(name: str, lo, hi, probs) -> list[str]:
    """How a model does on the random holdout, judged by hand."""
    truth = action_of_bounds(lo, hi)
    guess = action_probs(probs).argmax(1)
    s = scores(lo, hi, probs)
    lines = [f"  {name}: action accuracy {np.mean(guess == truth):.3f} ({int((guess == truth).sum())}"
             f"/{len(truth)}); {fmt(s)}"]
    known = (lo >= 1).astype(float)
    lines.append(f"    mean P(known) {1 - probs[:, 0].mean():.3f} vs share known {known.mean():.3f};"
                 f" mean P(suspend) {probs[:, 3].mean():.3f} vs share suspended"
                 f" {(lo == 3).mean():.3f}")
    header = "    judged \\ predicted " + " ".join(f"{a:>9}" for a in ACTIONS)
    lines.append(header)
    for t, a in enumerate(ACTIONS):
        row = [int(((truth == t) & (guess == g)).sum()) for g in range(3)]
        lines.append(f"    {a:>19} " + " ".join(f"{v:>9}" for v in row))
    return lines


# --- the second stage: agents' answers stacked on the first ------------------------------


def stack_inputs(probs, agent_p):
    return np.column_stack([_logit(1 - probs[:, 0]), _logit(probs[:, 3]), _logit(agent_p)])


def stack(first_oof, lo, hi, agent_p, repeats: int):
    """Cross-validated second stage over the labelled notes that have an agent answer: its
    out-of-fold probabilities and scores, and the first stage's scores on the same notes."""
    import pandas as pd

    frame = pd.DataFrame(stack_inputs(first_oof, agent_p), columns=["s_known", "s_sus", "agent"])
    oof, s = cross_validate(lambda: OrdinalLogit(STACK_L2), frame, lo, hi, repeats)
    return oof, s, scores(lo, hi, first_oof)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--long-days", type=int, default=LONG_DAYS)
    parser.add_argument("--no-hgb", action="store_true")
    parser.add_argument("--judged-date", action="store_true")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()

    frame, words = tf.table()
    labels = Labels(args.long_days)
    # The agents' answers are a second stage, not a feature of the first: most notes have none
    frame = frame[[c for c in frame.columns if not c.startswith("agent_")]]
    # Counted before the constant columns go: once every note has an answer, op_missing is one
    with_opus = int((frame["op_missing"] == 0).sum()) if "op_missing" in frame else len(frame)
    # A column that is one value throughout says nothing and only slows the fit
    frame = frame.loc[:, frame.nunique(dropna=False) > 1]
    labelled = labels.training(frame.index)
    held = labels.held_out(frame.index)
    x_lab = frame.loc[labelled]
    lo, hi = labels.arrays(labelled)
    n_anki = sum(labels.source[n] == "anki" for n in labelled)
    lines = [f"{len(frame)} notes, {len(frame.columns)} features; {len(labelled)} labelled"
             f" ({n_anki} in Anki, {len(labelled) - n_anki} by hand): "
             + ", ".join(f"{LEVELS[k]} {int(((lo == k) & (hi == k)).sum())}" for k in range(4))
             + f", schedule of either length {int(((lo == 1) & (hi == 2)).sum())}",
             f"random holdout: {len(labels.holdout)} queued, {len(held)} judged (never trained"
             " on)",
             f"Opus features present for {with_opus} notes",
             ""]

    lines.append("--- ordinal logistic regression, cross-validated by L2 strength ---")
    best = None
    for l2 in L2_GRID:
        oof, s = cross_validate(lambda: OrdinalLogit(l2), x_lab, lo, hi, args.repeats)
        lines.append(f"  l2 {l2:>6}: {fmt(s)}")
        if best is None or s["log_loss"] < best[2]["log_loss"]:
            best = (l2, oof, s)
    assert best is not None  # L2_GRID is never empty
    l2, oof_lin, s_lin = best
    lines.append(f"  chosen l2 {l2}" + ("  (the grid's edge: extend it)"
                                       if l2 in (L2_GRID[0], L2_GRID[-1]) else ""))
    lines.append("")

    oof_hgb, s_hgb = None, None
    if not args.no_hgb:
        oof_hgb, s_hgb = cross_validate(lambda: OrdinalTrees(), x_lab, lo, hi, args.repeats,
                                        prepared=False)
        lines.append(f"--- ordinal gradient-boosted trees: {fmt(s_hgb)}")
        lines.append(f"--- their average (first repeat): {fmt(scores(lo, hi, (oof_lin + oof_hgb) / 2))}")
        lines.append("")

    # Which model scores the notes: by the first repeat's out-of-fold log loss, alike for all
    oofs = {"linear": oof_lin}
    if oof_hgb is not None:
        oofs.update(trees=oof_hgb, average=(oof_lin + oof_hgb) / 2)
    choice = min(oofs, key=lambda k: scores(lo, hi, oofs[k])["log_loss"])
    lines.append(f"scoring with: {choice}")
    lines.append("")

    prep = Prepared().fit(x_lab)
    lin = OrdinalLogit(l2).fit(prep.transform(x_lab), lo, hi)
    names = prep.names
    order = np.argsort(-np.abs(lin.coef_))
    lines.append("--- what carries the ordinal model: standardized coefficients (+ = known) ---")
    for i in order[:35]:
        lines.append(f"  {lin.coef_[i]:+.3f}  {names[i]}")
    lines.append(f"  thresholds: {', '.join(f'{t:.2f}' for t in lin.thetas_)}")
    lines.append("")

    lines.append("--- leaving out one group at a time (ordinal logistic, same l2) ---")
    base = s_lin
    for group in tf.GROUPS:
        cols = [c for c in x_lab.columns if not c.startswith(group + "_")]
        if len(cols) == len(x_lab.columns):
            continue
        _, s = cross_validate(lambda: OrdinalLogit(l2), x_lab[cols], lo, hi, args.repeats)
        lines.append(f"  without {group:6}: auc_known {s['auc_known']:.3f}"
                     f" ({s['auc_known'] - base['auc_known']:+.3f}), log_loss {s['log_loss']:.3f}"
                     f" ({s['log_loss'] - base['log_loss']:+.3f}), spearman {s['spearman']:.3f}")
    lines.append("")

    trees = None
    if not args.no_hgb:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.inspection import permutation_importance
        from sklearn.model_selection import train_test_split

        known = (lo >= 1).astype(int)
        m = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250,
                                           l2_regularization=1.0, min_samples_leaf=20,
                                           random_state=0)
        xtr, xte, ytr, yte = train_test_split(x_lab.to_numpy(float), known, test_size=0.3,
                                              stratify=known, random_state=0)
        m.fit(xtr, ytr)
        imp = permutation_importance(m, xte, yte, scoring="roc_auc", n_repeats=10,
                                     random_state=0)
        lines.append("--- trees, known vs learn: permutation importance on a 30% hold-out (AUC"
                     " lost) ---")
        for i in np.argsort(-imp.importances_mean)[:20]:
            lines.append(f"  {imp.importances_mean[i]:.4f}  {x_lab.columns[i]}")
        lines.append("")
        trees = OrdinalTrees().fit(x_lab.to_numpy(float), lo, hi)

    if args.judged_date:
        # A suspended card's date is its last change, not when it was judged; and only the
        # Anki labels have a judging date at all
        dated = [n for n in labelled if labels.source[n] == "anki"
                 and labels.anki[n]["label"] != "suspend"]
        dlo, dhi = labels.arrays(dated)
        dates = np.array([
            (datetime.strptime(labels.anki[n]["judged"], "%Y-%m-%d") - datetime(2025, 6, 1)).days
            / 365.0 for n in dated])
        x_dated = frame.loc[dated]
        _, s0 = cross_validate(lambda: OrdinalLogit(l2, levels=3), x_dated, dlo, dhi,
                               args.repeats, levels=3)
        with_date = x_dated.assign(judged_years=dates)
        _, s = cross_validate(lambda: OrdinalLogit(l2, levels=3), with_date, dlo, dhi,
                              args.repeats, levels=3)
        p2 = Prepared().fit(with_date)
        m2 = OrdinalLogit(l2, levels=3).fit(p2.transform(with_date), dlo, dhi)
        coef = m2.coef_[p2.names.index("judged_years")]
        lines.append(f"--- the judging date, over the {len(dated)} Anki labels but suspend"
                     " (learn < short < long) ---")
        lines.append(f"  without it: {fmt(s0)}")
        lines.append(f"  with it:    {fmt(s)}")
        lines.append(f"  its standardized coefficient {coef:+.3f} (+ = later judgements lean to"
                     " 'known')")
        lines.append("")

    # Score every note
    x_all = prep.transform(frame)
    p_lin = lin.predict_proba(x_all)
    probs = p_lin
    if choice != "linear" and trees is not None:
        p_tree = trees.predict_proba(frame.to_numpy(float))
        probs = p_tree if choice == "trees" else (p_lin + p_tree) / 2
    index = {nid: i for i, nid in enumerate(frame.index)}
    oof = oofs[choice]
    for j, nid in enumerate(labelled):
        probs[index[nid]] = oof[j]
    first_stage = probs.copy()

    stacked: set[int] = set()
    agents = {r["nid"]: r for r in td.read_jsonl(td.data_file(AGENTS))}
    s_stack = None
    if agents:
        both = [j for j, nid in enumerate(labelled) if nid in agents]
        lines.append(f"--- second stage: the agents' P(known) over {len(agents)} notes,"
                     f" {len(both)} of them labelled ---")
        if len(both) >= 50:
            b = np.array(both)
            a_lab = np.array([agents[labelled[j]]["score"] for j in both], dtype=float)
            oof2, s_stack, s_first = stack(oof[b], lo[b], hi[b], a_lab, args.repeats)
            lines.append(f"  first stage alone: {fmt(s_first)}")
            lines.append(f"  stacked:           {fmt(s_stack)}")
            a_auc = scores(lo[b], hi[b], np.column_stack(
                [1 - a_lab, a_lab / 3, a_lab / 3, a_lab / 3])).get("auc_known")
            if a_auc is not None:
                lines.append(f"  the agents alone: auc_known {a_auc:.3f}")
            if s_stack["log_loss"] < s_first["log_loss"]:
                inputs = stack_inputs(oof[b], a_lab)
                import pandas as pd

                sframe = pd.DataFrame(inputs, columns=["s_known", "s_sus", "agent"])
                sp = Prepared().fit(sframe)
                second = OrdinalLogit(STACK_L2).fit(sp.transform(sframe), lo[b], hi[b])
                lines.append("  stacked coefficients: " + ", ".join(
                    f"{n} {c:+.3f}" for n, c in zip(sp.names, second.coef_)))
                labelled_pos = {labelled[j]: k for k, j in enumerate(both)}
                for nid, rec in agents.items():
                    if nid not in index:
                        continue
                    i = index[nid]
                    if nid in labelled_pos:
                        probs[i] = oof2[labelled_pos[nid]]
                    else:
                        row = stack_inputs(first_stage[i : i + 1], np.array([rec["score"]]))
                        probs[i] = second.predict_proba(sp.transform(pd.DataFrame(
                            row, columns=["s_known", "s_sus", "agent"])))[0]
                    stacked.add(nid)
                lines.append(f"  used for the {len(stacked)} notes the agents answered")
            else:
                lines.append("  not used: it does not cross-validate better")
        else:
            lines.append("  too few labelled notes with an answer to fit it (50 needed)")
        lines.append("")

    if held:
        hlo, hhi = labels.arrays(held)
        rows_h = np.array([index[n] for n in held])
        lines.append(f"--- the random holdout, {len(held)} judged by hand (never trained on) ---")
        lines += holdout_lines("linear", hlo, hhi, p_lin[rows_h])
        if trees is not None:
            p_tree_h = trees.predict_proba(frame.iloc[rows_h].to_numpy(float))
            lines += holdout_lines("trees", hlo, hhi, p_tree_h)
            lines += holdout_lines("average", hlo, hhi, (p_lin[rows_h] + p_tree_h) / 2)
        lines += holdout_lines(f"as scored ({choice}"
                               + (", stacked where answered" if stacked else "") + ")",
                               hlo, hhi, probs[rows_h])
        lines.append("")

    contributions = x_all * lin.coef_
    rows: list[dict[str, Any]] = []
    for nid, i in index.items():
        p = probs[i]
        top_pos = np.argsort(-contributions[i])[:3]
        top_neg = np.argsort(contributions[i])[:3]
        source = labels.source.get(nid)
        bounds: Optional[tuple[int, int]] = labels.bounds.get(nid)
        rows.append({
            "nid": int(nid),
            "p": [round(float(v), 4) for v in p],
            "p_known": round(float(1 - p[0]), 4),
            "p_suspend": round(float(p[3]), 4),
            "expected": round(float(p @ np.arange(4)), 3),
            "labelled": source is not None,
            "label_source": source,
            "label_level": list(bounds) if bounds else None,
            "stacked": nid in stacked,
            "for": [names[k] for k in top_pos if contributions[i][k] > 0],
            "against": [names[k] for k in top_neg if contributions[i][k] < 0],
        })
    td.write_jsonl(td.data_file("predictions.jsonl"), rows)
    meta = {"model": choice, "l2": l2, "long_days": args.long_days,
            "cv": {"linear": s_lin, "trees": s_hgb, "stacked": s_stack}}
    td.data_file("model_meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    unl = [r for r in rows if not r["labelled"]]
    lines.append("--- the unjudged notes as scored ---")
    for low, high in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, .9), (.9, .95), (.95, 1.01)):
        n = sum(low <= r["p_known"] < high for r in unl)
        lines.append(f"  P(known) {low:.2f}-{min(high, 1):.2f}: {n:>6}")
    lines.append("  expected levels: " + ", ".join(
        f"{LEVELS[k]} {sum(r['p'][k] for r in unl):.0f}" for k in range(4)))
    path = td.report_file("model.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
