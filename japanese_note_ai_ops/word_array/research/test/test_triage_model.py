"""The triage model's censored ordinal fit: a hand label of Schedule, which has no interval,
is fitted as "short or long" rather than forced to either.

The model needs numpy, scipy and scikit-learn, which the research scripts install for themselves
and the dev requirements do not carry; without them this file is skipped.
"""

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("sklearn")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import triage_model as tm  # noqa: E402


def test_a_censored_label_fits_like_the_levels_it_allows():
    """Labels drawn from a latent scale; the middle two levels given only as 'one of them'."""
    rng = np.random.default_rng(0)
    x = rng.normal(size=(600, 2))
    latent = x @ np.array([1.5, -1.0]) + rng.logistic(size=600)
    level = np.digitize(latent, [-1.5, 0.0, 1.5])
    lo, hi = level.copy(), level.copy()
    middle = (level == 1) | (level == 2)
    lo[middle], hi[middle] = 1, 2
    model = tm.OrdinalLogit(l2=0.1).fit(x, lo, hi)
    assert model.coef_[0] > 0.8 and model.coef_[1] < -0.5
    probs = model.predict_proba(x)
    assert np.allclose(probs.sum(1), 1, atol=1e-6)
    assert tm.range_prob(probs, lo, hi).min() > 0
    exact = tm.OrdinalLogit(l2=0.1).fit(x, level, level)
    assert np.sign(exact.coef_).tolist() == np.sign(model.coef_).tolist()


def test_the_trees_leave_a_censored_label_out_of_the_step_it_spans():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(300, 1))
    lo = np.digitize(x[:, 0], [-0.8, 0.0, 0.8])
    hi = lo.copy()
    lo[lo == 2], hi[hi == 1] = 1, 2  # every middle label is 'short or long'
    trees = tm.OrdinalTrees().fit(x, lo, hi)
    # The short/long step's model saw only the labels on either side of it
    assert trees.models[1].n_features_in_ == 1
    probs = trees.predict_proba(x)
    assert np.allclose(probs.sum(1), 1, atol=1e-6)


def test_hand_schedule_is_censored_and_anki_schedule_is_not():
    assert tm.bounds_of({"label": "schedule"}, 900) == (1, 2)
    assert tm.bounds_of({"label": "schedule", "interval": 1000}, 900) == (2, 2)
    assert tm.bounds_of({"label": "learn"}, 900) == (0, 0)
    assert tm.bounds_of({"label": "suspend"}, 900) == (3, 3)
