"""The triage model: how sure the user would be that they know each word, fitted on their labels.

    py -3.10 word_array/research/triage_model.py [--no-hgb] [--judged-date]

The labels are one ordered scale, the user's certainty (triage_labels.py): learn < schedule
short (an interval under `--long-days`) < schedule long < suspend. Two models of that scale:

- an ordinal logistic regression (cumulative logit, L2, its strength picked by cross-validation)
  over standardized features, missing values set to the median with an `_isnan` column beside
  them: interpretable, its coefficients are what carries it;
- small gradient-boosted trees, one binary model per step of the scale (level >= 1, >= 2, >= 3,
  Frank and Hall's ordinal trees), for what the linear model cannot bend to.

Both are scored by repeated stratified 5-fold cross-validation: AUC at each step of the scale
(known at all, i.e. not learn; long or suspend; suspend), the log loss of the four levels, and
Spearman's rho between the expected level and the label. Which features carry it: the ordinal
model's standardized coefficients, the trees' permutation importance, and the loss when a whole
group (triage_features.GROUPS) is left out. `--judged-date` adds when each word was judged, to
see whether the user's standard moved over time once the words themselves are accounted for.

The model that cross-validates better (or their average, when that is better still) scores
every note: out-of-fold for the labelled ones, fitted on all labels for the rest. Writes
`predictions.jsonl` (per note: the four levels' probabilities, P(known), the expected level and
the features pushing it most either way) and `reports/model.txt`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime

import numpy as np

import triage_data as td
import triage_features as tf

LEVELS = ("learn", "schedule_short", "schedule_long", "suspend")
LONG_DAYS = 900
L2_GRID = (0.3, 1.0, 3.0, 10.0, 30.0)


def level_of(label: dict, long_days: int) -> int:
    if label["label"] == "learn":
        return 0
    if label["label"] == "suspend":
        return 3
    return 2 if label["interval"] >= long_days else 1


# --- the ordinal logistic model ----------------------------------------------------------


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


class OrdinalLogit:
    """P(level >= k) = sigmoid(x.w - theta_k), k = 1..K-1, thetas increasing; L2 on w."""

    def __init__(self, l2: float = 1.0, levels: int = 4) -> None:
        self.l2 = l2
        self.k = levels

    def _unpack(self, params):
        p = self.p
        w = params[:p]
        thetas = params[p] + np.concatenate([[0.0], np.cumsum(np.exp(params[p + 1:]))])
        return w, thetas

    def fit(self, x, y, sample_weight=None):
        from scipy.optimize import minimize

        n, self.p = x.shape
        sw = np.ones(n) if sample_weight is None else sample_weight
        onehot = np.zeros((n, self.k))
        onehot[np.arange(n), y] = 1

        def loss(params):
            w, thetas = self._unpack(params)
            z = x @ w
            cum = _sigmoid(z[:, None] - thetas[None, :])  # P(y >= k), k = 1..K-1
            upper = np.hstack([np.ones((n, 1)), cum])
            lower = np.hstack([cum, np.zeros((n, 1))])
            probs = np.clip(upper - lower, 1e-12, 1)
            nll = -(sw * np.log((probs * onehot).sum(1))).sum()
            # gradients
            dens = cum * (1 - cum)
            du = np.hstack([np.zeros((n, 1)), dens])  # d upper / d z
            dl = np.hstack([dens, np.zeros((n, 1))])
            p_obs = (probs * onehot).sum(1)
            dz = -sw * ((du - dl) * onehot).sum(1) / p_obs
            gw = x.T @ dz + self.l2 * w
            # d/d theta_j of -log p: upper uses theta_{k-1}, lower uses theta_k
            gtheta = np.zeros(self.k - 1)
            for j in range(self.k - 1):
                in_upper = onehot[:, j + 1]  # level j+1 has theta_j as its upper bound
                in_lower = onehot[:, j]      # level j has theta_j as its lower bound
                gtheta[j] = (sw * (in_upper * dens[:, j] - in_lower * dens[:, j]) / p_obs).sum()
            g0 = gtheta.sum()
            gdelta = np.array([gtheta[i:].sum() * math.exp(params[self.p + i])
                               for i in range(1, self.k - 1)])
            return nll + 0.5 * self.l2 * (w @ w), np.concatenate([gw, [g0], gdelta])

        init = np.concatenate([np.zeros(self.p), [-1.0], np.zeros(self.k - 2)])
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
    """Frank and Hall: one gradient-boosted binary model per step, P(level >= k)."""

    def __init__(self, levels: int = 4, seed: int = 0) -> None:
        self.k = levels
        self.seed = seed

    def fit(self, x, y):
        from sklearn.ensemble import HistGradientBoostingClassifier

        self.models = []
        for step in range(1, self.k):
            m = HistGradientBoostingClassifier(
                max_depth=3, learning_rate=0.05, max_iter=250, l2_regularization=1.0,
                min_samples_leaf=20, random_state=self.seed)
            m.fit(x, (y >= step).astype(int))
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


def scores(y, probs) -> dict:
    from scipy.stats import spearmanr
    from sklearn.metrics import roc_auc_score

    out = {}
    for step, name in ((1, "auc_known"), (2, "auc_long"), (3, "auc_suspend")):
        truth = (y >= step).astype(int)
        if 0 < truth.sum() < len(truth):
            out[name] = roc_auc_score(truth, probs[:, step:].sum(1))
    out["log_loss"] = float(-np.mean(np.log(probs[np.arange(len(y)), y])))
    expected = probs @ np.arange(probs.shape[1])
    out["spearman"] = float(spearmanr(expected, y).correlation)
    return out


def cross_validate(make, frame, y, repeats: int = 2, seed: int = 0, prepared: bool = True):
    """Out-of-fold probabilities of the first repeat, and the mean scores over all."""
    from sklearn.model_selection import StratifiedKFold

    runs, first = [], None
    for r in range(repeats):
        oof = np.zeros((len(y), 4))
        for train, test in StratifiedKFold(5, shuffle=True, random_state=seed + r).split(frame, y):
            if prepared:
                prep = Prepared().fit(frame.iloc[train])
                xtr, xte = prep.transform(frame.iloc[train]), prep.transform(frame.iloc[test])
            else:
                xtr, xte = frame.iloc[train].to_numpy(float), frame.iloc[test].to_numpy(float)
            oof[test] = make().fit(xtr, y[train]).predict_proba(xte)
        runs.append(scores(y, oof))
        if first is None:
            first = oof
    mean = {k: float(np.mean([s[k] for s in runs if k in s])) for k in runs[0]}
    return first, mean


def fmt(s: dict) -> str:
    return ", ".join(f"{k} {v:.3f}" for k, v in s.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--long-days", type=int, default=LONG_DAYS)
    parser.add_argument("--no-hgb", action="store_true")
    parser.add_argument("--judged-date", action="store_true")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()

    frame, words = tf.table()
    labels = {r["nid"]: r for r in td.read_jsonl(td.data_file("labels.jsonl"))}
    # A column that is one value throughout says nothing and only slows the fit
    frame = frame.loc[:, frame.nunique(dropna=False) > 1]
    labelled = [nid for nid in frame.index if nid in labels]
    x_lab = frame.loc[labelled]
    y = np.array([level_of(labels[nid], args.long_days) for nid in labelled])
    lines = [f"{len(frame)} notes, {len(frame.columns)} features; {len(labelled)} labelled: "
             + ", ".join(f"{LEVELS[k]} {int((y == k).sum())}" for k in range(4)),
             f"Opus features present for {int((frame.get('op_missing', 0) == 0).sum())} notes",
             ""]

    lines.append("--- ordinal logistic regression, cross-validated by L2 strength ---")
    best = None
    for l2 in L2_GRID:
        oof, s = cross_validate(lambda: OrdinalLogit(l2), x_lab, y, args.repeats)
        lines.append(f"  l2 {l2:>5}: {fmt(s)}")
        if best is None or s["log_loss"] < best[2]["log_loss"]:
            best = (l2, oof, s)
    l2, oof_lin, s_lin = best
    lines.append(f"  chosen l2 {l2}")
    lines.append("")

    oof_hgb, s_hgb = None, None
    if not args.no_hgb:
        oof_hgb, s_hgb = cross_validate(lambda: OrdinalTrees(), x_lab, y, args.repeats,
                                        prepared=False)
        lines.append(f"--- ordinal gradient-boosted trees: {fmt(s_hgb)}")
        avg = (oof_lin + oof_hgb) / 2
        s_avg = scores(y, avg)
        lines.append(f"--- their average (first repeat): {fmt(s_avg)}")
        lines.append("")

    # Which model scores the notes
    choice = "linear"
    if s_hgb is not None:
        s_avg = scores(y, (oof_lin + oof_hgb) / 2)
        candidates = {"linear": scores(y, oof_lin)["log_loss"],
                      "trees": scores(y, oof_hgb)["log_loss"], "average": s_avg["log_loss"]}
        choice = min(candidates, key=candidates.get)
    lines.append(f"scoring with: {choice}")
    lines.append("")

    prep = Prepared().fit(x_lab)
    lin = OrdinalLogit(l2).fit(prep.transform(x_lab), y)
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
        _, s = cross_validate(lambda: OrdinalLogit(l2), x_lab[cols], y, args.repeats)
        lines.append(f"  without {group:6}: auc_known {s['auc_known']:.3f}"
                     f" ({s['auc_known'] - base['auc_known']:+.3f}), log_loss {s['log_loss']:.3f}"
                     f" ({s['log_loss'] - base['log_loss']:+.3f}), spearman {s['spearman']:.3f}")
    lines.append("")

    trees = None
    if not args.no_hgb:
        from sklearn.inspection import permutation_importance
        from sklearn.ensemble import HistGradientBoostingClassifier

        known = (y >= 1).astype(int)
        m = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250,
                                           l2_regularization=1.0, min_samples_leaf=20,
                                           random_state=0)
        from sklearn.model_selection import train_test_split

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
        trees = OrdinalTrees().fit(x_lab.to_numpy(float), y)

    if args.judged_date:
        dates = np.array([
            (datetime.strptime(labels[nid]["judged"], "%Y-%m-%d") - datetime(2025, 6, 1)).days
            / 365.0 for nid in labelled])
        with_date = x_lab.assign(judged_years=dates)
        _, s = cross_validate(lambda: OrdinalLogit(l2), with_date, y, args.repeats)
        p2 = Prepared().fit(with_date)
        m2 = OrdinalLogit(l2).fit(p2.transform(with_date), y)
        coef = m2.coef_[p2.names.index("judged_years")]
        lines.append(f"--- with the judging date: {fmt(s)}; its standardized coefficient"
                     f" {coef:+.3f} (+ = later judgements lean to 'known')")
        lines.append("")

    # Score every note
    x_all = prep.transform(frame)
    p_lin = lin.predict_proba(x_all)
    probs = p_lin
    if choice != "linear" and trees is not None:
        p_tree = trees.predict_proba(frame.to_numpy(float))
        probs = p_tree if choice == "trees" else (p_lin + p_tree) / 2
    oof = {"linear": oof_lin, "trees": oof_hgb,
           "average": None if oof_hgb is None else (oof_lin + oof_hgb) / 2}[choice]
    index = {nid: i for i, nid in enumerate(frame.index)}
    for j, nid in enumerate(labelled):
        probs[index[nid]] = oof[j]
    contributions = x_all * lin.coef_
    rows = []
    for nid, i in index.items():
        p = probs[i]
        top_pos = np.argsort(-contributions[i])[:3]
        top_neg = np.argsort(contributions[i])[:3]
        rows.append({
            "nid": int(nid),
            "p": [round(float(v), 4) for v in p],
            "p_known": round(float(1 - p[0]), 4),
            "p_suspend": round(float(p[3]), 4),
            "expected": round(float(p @ np.arange(4)), 3),
            "labelled": nid in labels,
            "label_level": level_of(labels[nid], args.long_days) if nid in labels else None,
            "for": [names[k] for k in top_pos if contributions[i][k] > 0],
            "against": [names[k] for k in top_neg if contributions[i][k] < 0],
        })
    td.write_jsonl(td.data_file("predictions.jsonl"), rows)
    meta = {"model": choice, "l2": l2, "long_days": args.long_days,
            "cv": {"linear": s_lin, "trees": s_hgb}}
    td.data_file("model_meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    unl = [r for r in rows if not r["labelled"]]
    lines.append("--- the unjudged notes as scored ---")
    for lo, hi in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, .9), (.9, .95), (.95, 1.01)):
        n = sum(lo <= r["p_known"] < hi for r in unl)
        lines.append(f"  P(known) {lo:.2f}-{min(hi, 1):.2f}: {n:>6}")
    lines.append("  expected levels: " + ", ".join(
        f"{LEVELS[k]} {sum(r['p'][k] for r in unl):.0f}" for k in range(4)))
    path = td.report_file("model.txt")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
